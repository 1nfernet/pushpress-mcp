"""Interactive login: saves the API session (never credentials) to state/session.json.

The member app authenticates API calls with a bearer JWT. We open the app,
let you log in, and capture the token from the app's own API requests.

Usage: uv run python -m pushpress_mcp.auth
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import SESSION_PATH, STATE_DIR
from .endpoints import API_BASE_URL, GRAPHQL_PATH, LOGIN_URL, token_expiry
from .errors import RELOGIN_COMMAND, SessionExpiredError


def load_session() -> dict[str, Any]:
    """Return {"bearer_token": ..., "storage_state": {...}}."""
    if not SESSION_PATH.exists():
        raise SessionExpiredError(f"No saved PushPress session. Run `{RELOGIN_COMMAND}`.")
    return json.loads(SESSION_PATH.read_text())


def save_session(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    # Create with 0600 from the start so the token is never world-readable.
    fd = os.open(SESSION_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.chmod(SESSION_PATH, 0o600)


def login() -> None:
    from playwright.sync_api import Request, sync_playwright

    captured: dict[str, str] = {}

    def on_request(req: Request) -> None:
        auth = req.headers.get("authorization", "")
        if req.url.startswith(API_BASE_URL + GRAPHQL_PATH) and auth.startswith("Bearer "):
            captured["token"] = auth.removeprefix("Bearer ")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        context.on("request", on_request)
        page = context.new_page()
        page.goto(LOGIN_URL)
        print("Log in in the browser window (including any 2FA), then open the Workouts tab.")
        input("When you can see your workouts, press Enter here to save the session... ")
        while "token" not in captured:
            input("No API traffic seen yet. Open the Workouts tab in the browser, then press Enter... ")
        state = {"bearer_token": captured["token"], "storage_state": context.storage_state()}
        browser.close()
    save_session(state)
    expiry = token_expiry(state)
    until = f", valid until {expiry:%Y-%m-%d %H:%M} UTC" if expiry else ""
    print(f"Session saved to {SESSION_PATH}{until}. Credentials were not stored.")


if __name__ == "__main__":
    login()
