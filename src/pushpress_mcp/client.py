"""Authenticated, polite HTTP client for the PushPress member API.

Sequential only, at most ~1 request/second, small retry budget. Never logs
tokens, headers, query strings or request bodies.

`uv run python -m pushpress_mcp.client` fetches this week's workouts and prints
them (build step 3 smoke check).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from . import endpoints
from .auth import load_session
from .errors import APIError, SessionExpiredError

log = logging.getLogger(__name__)

MIN_INTERVAL_S = 1.0  # spec: 300-500ms delay, max ~1 request per second
MAX_ATTEMPTS = 3
RETRY_STATUSES = {429, 500, 502, 503, 504}
USER_AGENT = "pushpress-mcp/0.1 (personal, read-only)"


class PushPressClient:
    def __init__(
        self,
        session: dict[str, Any] | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        min_interval: float = MIN_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        state = session if session is not None else load_session()
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **endpoints.auth_headers(state)}
        self._http = httpx.Client(
            base_url=endpoints.API_BASE_URL,
            headers=headers,
            timeout=30.0,
            follow_redirects=True,
            transport=transport,
        )
        self._min_interval = min_interval
        self._sleep = sleep
        self._last_request = 0.0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PushPressClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._request("GET", url, params=params)

    def post_json(self, url: str, body: Any) -> Any:
        """POST for read-only queries (GraphQL). Callers must never send mutations."""
        return self._request("POST", url, json=body)

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        # Only the path goes into messages/logs; queries, headers and bodies may carry tokens.
        path = urlsplit(urljoin(endpoints.API_BASE_URL, url)).path
        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            try:
                resp = self._http.request(method, url, **kwargs)
            except httpx.TransportError as e:
                last_error = type(e).__name__
                log.warning("%s %s failed (%s), attempt %d/%d", method, path, last_error, attempt, MAX_ATTEMPTS)
                self._sleep(2**attempt)
                continue
            if resp.status_code in (401, 403):
                raise SessionExpiredError()
            if resp.status_code in RETRY_STATUSES:
                last_error = f"HTTP {resp.status_code}"
                log.warning("%s %s -> %d, attempt %d/%d", method, path, resp.status_code, attempt, MAX_ATTEMPTS)
                self._sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                raise APIError(f"{method} {path} returned HTTP {resp.status_code}.")
            if "html" in resp.headers.get("content-type", ""):
                # An expired session usually bounces to a login page.
                raise SessionExpiredError()
            try:
                return resp.json()
            except ValueError as e:
                raise APIError(f"{method} {path} did not return JSON.") from e
        raise APIError(f"{method} {path} failed after {MAX_ATTEMPTS} attempts ({last_error}).")


def main() -> None:
    import datetime as dt
    import json

    from .errors import PushPressError

    today = dt.date.today()
    start = today - dt.timedelta(days=today.weekday())
    end = start + dt.timedelta(days=6)
    try:
        with PushPressClient() as client:
            workouts = [w for raw in endpoints.fetch_workouts(client, start, end) for w in endpoints.parse_workouts(raw)]
    except PushPressError as e:
        raise SystemExit(str(e)) from None
    print(json.dumps([w.public() for w in workouts], indent=2))


if __name__ == "__main__":
    main()
