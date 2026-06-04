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

import json
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
                idempotent: bool = True) -> object:
        """Authenticated call. Returns parsed JSON on 2xx, else raises
        UpstreamError. Refreshes the JWT once on 401.

        Retry policy:
          - 429 is always retryable: the request was rate-limited, i.e.
            provably NOT processed, so re-sending creates no duplicate.
          - 5xx is retried ONLY when `idempotent` is True. For a
            non-idempotent op (e.g. POST /documents), a 5xx is ambiguous —
            the server may have created the document before failing — so we
            do NOT retry; that would risk a second irreversible document.
        """
        url = self.cfg.base_url + path
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
