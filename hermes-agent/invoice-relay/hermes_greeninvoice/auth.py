"""JWT token acquisition + cache for the GreenInvoice API.

`POST /account/token` with {id, secret} returns a JWT valid ~1 hour. We
cache it in-process and refresh when it's within TOKEN_SKEW_SECONDS of
expiry. Thread-safe: a single lock guards refresh so concurrent workers
don't stampede the token endpoint.

The token endpoint response shape (observed):
  { "token": "<jwt>", "expires": <unix_epoch_seconds> }
We tolerate a missing/!int `expires` by falling back to a conservative
50-minute TTL from now.
"""

from __future__ import annotations

import threading
import time

from .errors import UpstreamError

TOKEN_SKEW_SECONDS = 60
FALLBACK_TTL_SECONDS = 50 * 60


class TokenCache:
    def __init__(self, *, api_key_id: str, api_key_secret: str,
                 http_post_json):
        """`http_post_json(path, body, *, authed)` -> (status, dict). The
        client injects its own transport so this module has no urllib
        dependency and is trivially testable."""
        self._id = api_key_id
        self._secret = api_key_secret
        self._post = http_post_json
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_epoch: int = 0

    def _valid(self) -> bool:
        return bool(self._token) and (
            time.time() < self._expires_epoch - TOKEN_SKEW_SECONDS
        )

    def get(self, *, force: bool = False) -> str:
        if not force and self._valid():
            return self._token  # type: ignore[return-value]
        with self._lock:
            # Re-check under lock (another thread may have refreshed).
            if not force and self._valid():
                return self._token  # type: ignore[return-value]
            status, data = self._post(
                "/account/token",
                {"id": self._id, "secret": self._secret},
                authed=False,
            )
            if status != 200 or not isinstance(data, dict):
                raise UpstreamError(
                    "token_request_failed",
                    detail=f"status={status}",
                    status=status,
                )
            token = data.get("token")
            if not isinstance(token, str) or not token:
                raise UpstreamError(
                    "token_response_malformed", detail="no token field"
                )
            expires = data.get("expires")
            now = int(time.time())
            if isinstance(expires, (int, float)) and expires > now:
                self._expires_epoch = int(expires)
            else:
                self._expires_epoch = now + FALLBACK_TTL_SECONDS
            self._token = token
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_epoch = 0

    def invalidate_if_current(self, token_value: str) -> None:
        """Drop the cached token only if it still equals `token_value`.
        Prevents a worker that got a 401 on an OLD token from clobbering a
        token a concurrent worker just refreshed (avoids refresh storms)."""
        with self._lock:
            if self._token == token_value:
                self._token = None
                self._expires_epoch = 0
