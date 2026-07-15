"""GreenInvoice (Morning) HTTP client — stdlib only.

Owns the JWT token cache and is the single chokepoint for every call to
the GreenInvoice API. Responsibilities:
  - inject the Authorization header (Bearer <jwt>),
  - throttle to <= the API's ~3 req/s ceiling (min inter-request gap),
  - retry transient failures (429, 5xx) with bounded exponential backoff,
  - refresh the token once on a 401 and retry the call,
  - convert non-2xx into UpstreamError with a SAFE (non-leaky) detail.

No third-party deps (urllib). The daemon runs under a hardened systemd
unit with PrivateTmp etc.; keeping the dependency surface at zero matters.

Dry-run note: this client is only constructed/called in LIVE mode. In
dry-run the handler short-circuits before reaching upstream, so the whole
validate -> rate-limit -> audit pipeline runs with no credentials.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .auth import TokenCache
from .config import Config
from .errors import UpstreamError

# Retry policy for transient upstream failures.
_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 0.5

_USER_AGENT = "hermes-greeninvoice/0.1 (+jetson)"

# Hosts we will POST an uploaded invoice file to. The presigned upload URL is
# returned by an AUTHENTICATED GreenInvoice call, but we still pin the target
# host (defence in depth against a spoofed/compromised response redirecting
# our file elsewhere). S3 buckets live under amazonaws.com; the greeninvoice
# file-upload gateways are also permitted.
_UPLOAD_HOST_SUFFIXES = (
    ".amazonaws.com",
    ".greeninvoice.co.il",
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow ANY redirect on the S3 upload POST — a redirect could
    aim our file bytes at a host we never validated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpstreamError("upload_redirect_refused", detail=str(code))


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _validate_upload_url(url: str) -> None:
    """SSRF guard for the presigned S3 URL. https only, no userinfo, a pinned
    host suffix, and every resolved IP must be public (blocks loopback /
    private / link-local, i.e. DNS-rebinding targets)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise UpstreamError("upload_url_rejected", detail="scheme")
    if parts.username or parts.password:
        raise UpstreamError("upload_url_rejected", detail="userinfo")
    host = parts.hostname or ""
    if not host or not any(host == s[1:] or host.endswith(s) for s in _UPLOAD_HOST_SUFFIXES):
        raise UpstreamError("upload_url_rejected", detail="host")
    port = parts.port
    if port is not None and port != 443:
        raise UpstreamError("upload_url_rejected", detail="port")
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise UpstreamError("upload_url_rejected", detail="dns")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
                or ip.is_multicast or ip.is_unspecified:
            raise UpstreamError("upload_url_rejected", detail="ip")


class GreenInvoiceClient:
    def __init__(self, cfg: Config):
        if not cfg.api_key_id or not cfg.api_key_secret:
            raise UpstreamError("missing_credentials",
                                detail="GI_API_KEY_ID / GI_API_KEY_SECRET unset")
        self.cfg = cfg
        self._throttle_lock = threading.Lock()
        self._last_call_monotonic = 0.0
        self._tokens = TokenCache(
            api_key_id=cfg.api_key_id,
            api_key_secret=cfg.api_key_secret,
            http_post_json=self._post_json_unauthed,
        )

    # ---- throttle --------------------------------------------------------

    def _throttle(self) -> None:
        min_gap = self.cfg.min_request_interval_ms / 1000.0
        with self._throttle_lock:
            now = time.monotonic()
            wait = (self._last_call_monotonic + min_gap) - now
            if wait > 0:
                time.sleep(wait)
            self._last_call_monotonic = time.monotonic()

    # ---- low-level HTTP --------------------------------------------------

    def _http(self, method: str, url: str, *, headers: dict,
              body: dict | None) -> tuple[int, object]:
        """Perform ONE HTTP request. Returns (status, parsed_json).

        Raises UpstreamError only for network-level failures or
        undecodable bodies. HTTP error *statuses* (4xx/5xx) are returned
        as (status, data) so the caller can decide retry/auth-refresh.
        """
        data_bytes = None
        req_headers = dict(headers)
        req_headers["User-Agent"] = _USER_AGENT
        req_headers["Accept"] = "application/json"
        if body is not None:
            data_bytes = json.dumps(body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            url, data=data_bytes, headers=req_headers, method=method,
        )
        self._throttle()
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.http_timeout_seconds) as resp:
                status = resp.getcode()
                raw = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read() if hasattr(e, "read") else b""
        except urllib.error.URLError as e:
            raise UpstreamError("network_error", detail=str(getattr(e, "reason", e))[:120])
        except (TimeoutError, OSError) as e:
            raise UpstreamError("network_error", detail=str(e)[:120])

        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError:
            # Body present but not JSON. Surface a safe, truncated marker.
            return status, {"_nonjson": raw[:200].decode("utf-8", "replace")}

    # ---- token-endpoint transport (used by TokenCache) -------------------

    def _post_json_unauthed(self, path: str, body: dict, *, authed: bool):
        url = self.cfg.base_url + path
        for attempt in range(_MAX_RETRIES + 1):
            status, data = self._http("POST", url, headers={}, body=body)
            if status in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            return status, data
        return status, data  # pragma: no cover (loop always returns)

    # ---- authed request with retry + one-shot re-auth --------------------

    def request(self, method: str, path: str, *,
                body: dict | None = None,
                params: dict | None = None,
                idempotent: bool = True,
                base_url: str | None = None) -> object:
        """Authenticated call. Returns parsed JSON on 2xx, else raises
        UpstreamError. Refreshes the JWT once on 401.

        `base_url` overrides the default API base — used for the expenses
        file-upload gateway, which lives on a different host.

        Retry policy:
          - 429 is always retryable: the request was rate-limited, i.e.
            provably NOT processed, so re-sending creates no duplicate.
          - 5xx is retried ONLY when `idempotent` is True. For a
            non-idempotent op (e.g. POST /documents), a 5xx is ambiguous —
            the server may have created the document before failing — so we
            do NOT retry; that would risk a second irreversible document.
        """
        url = (base_url or self.cfg.base_url) + path
        if params:
            url = url + "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )

        reauthed = False
        last_status = None
        last_detail = ""
        for attempt in range(_MAX_RETRIES + 1):
            token = self._tokens.get()
            headers = {"Authorization": f"Bearer {token}"}
            status, data = self._http(method, url, headers=headers, body=body)
            last_status = status

            if 200 <= status < 300:
                return data

            if status == 401 and not reauthed:
                # Token may have been revoked early; force a refresh once.
                # Compare-and-invalidate so we don't clobber a token another
                # worker just refreshed.
                self._tokens.invalidate_if_current(token)
                reauthed = True
                continue

            retryable = status == 429 or (idempotent and status in _RETRY_STATUSES)
            if retryable and attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue

            last_detail = _safe_error_detail(data)
            break

        raise UpstreamError("api_error", detail=last_detail, status=last_status)

    # ---- convenience verbs ----------------------------------------------

    def get(self, path: str, *, params: dict | None = None) -> object:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict, *, idempotent: bool = True) -> object:
        return self.request("POST", path, body=body, idempotent=idempotent)

    def put(self, path: str, body: dict, *, idempotent: bool = True) -> object:
        return self.request("PUT", path, body=body, idempotent=idempotent)

    # ---- expense file upload (presigned S3) ------------------------------

    def get_upload_url(self, *, source: int = 5) -> object:
        """Step 1 of the expense file upload: an AUTHED GET to the file-upload gateway,
        returning a presigned S3 POST ({url, fields}). Lives on a different host from the
        JSON API.

        CRITICAL: the routing to EXPENSE OCR is baked into the presigned URL at THIS step,
        via two QUERY PARAMS — `context=expense` and `data` (URL-encoded JSON). It is NOT a
        request body and there is no later "commit" call. A GET body is dropped by the
        gateway, and without `context=expense` the object is merely STORED (S3 returns 204)
        and never handed to OCR, so no draft is ever created. That was the original bug:
        we sent `{"source":5}` as a body with no context, the upload "succeeded", and the
        draft never appeared. `source=5` ("API upload") stays mandatory; it goes inside
        `data`."""
        return self.request(
            "GET", "/file-upload/v1/url",
            params={"context": "expense",
                    "data": json.dumps({"source": source}, separators=(",", ":"))},
            base_url=self.cfg.file_upload_base_url,
            idempotent=True,
        )

    def upload_file_to_s3(self, url: str, fields: dict, *, filename: str,
                          content_type: str, data: bytes) -> None:
        """Step 2: POST the file as multipart/form-data to the presigned S3
        `url`. NO bearer auth (the presigned `fields` ARE the auth), NO
        redirects, host+IP validated. Treats (url, fields) as one opaque grant.
        Raises UpstreamError on any non-2xx / network / validation failure."""
        if not isinstance(url, str) or not isinstance(fields, dict):
            raise UpstreamError("upload_url_rejected", detail="shape")
        _validate_upload_url(url)

        boundary = "----hermesgi" + os.urandom(18).hex()
        bb = boundary.encode("ascii")
        crlf = b"\r\n"
        chunks: list[bytes] = []
        # Presigned POST form fields FIRST (order preserved), file part LAST —
        # S3 requires the `file` field to be the final part.
        for k, v in fields.items():
            chunks.append(b"--" + bb + crlf)
            chunks.append(
                b'Content-Disposition: form-data; name="' + str(k).encode("utf-8") + b'"'
                + crlf + crlf)
            chunks.append(str(v).encode("utf-8") + crlf)
        chunks.append(b"--" + bb + crlf)
        chunks.append(
            b'Content-Disposition: form-data; name="file"; filename="'
            + filename.encode("utf-8") + b'"' + crlf)
        chunks.append(b"Content-Type: " + content_type.encode("ascii") + crlf + crlf)
        chunks.append(data + crlf)
        chunks.append(b"--" + bb + b"--" + crlf)
        payload = b"".join(chunks)

        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "User-Agent": _USER_AGENT,
            },
        )
        self._throttle()
        try:
            with _NO_REDIRECT_OPENER.open(req, timeout=self.cfg.http_timeout_seconds) as resp:
                status = resp.getcode()
        except urllib.error.HTTPError as e:
            raise UpstreamError("api_error", detail=f"s3_status={e.code}", status=e.code)
        except urllib.error.URLError as e:
            raise UpstreamError("network_error", detail=str(getattr(e, "reason", e))[:120])
        except (TimeoutError, OSError) as e:
            raise UpstreamError("network_error", detail=str(e)[:120])
        if not (200 <= status < 300):
            raise UpstreamError("api_error", detail=f"s3_status={status}", status=status)


def _safe_error_detail(data: object) -> str:
    """Extract a short, non-secret error description from an API error
    body. GreenInvoice errors look like {"errorCode": N, "errorMessage":
    "..."}; we surface only those fields, truncated."""
    if isinstance(data, dict):
        code = data.get("errorCode")
        msg = data.get("errorMessage") or data.get("_nonjson")
        bits = []
        if code is not None:
            bits.append(f"code={code}")
        if isinstance(msg, str) and msg:
            bits.append(msg[:160])
        if bits:
            return " ".join(bits)
    return "non-2xx response"
