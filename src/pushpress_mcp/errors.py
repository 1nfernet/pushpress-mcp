from __future__ import annotations

RELOGIN_COMMAND = "uv run python -m pushpress_mcp.auth"


class PushPressError(RuntimeError):
    """Base class. Messages are safe to show to the user: no tokens or full URLs."""


class SessionExpiredError(PushPressError):
    def __init__(self, message: str = f"Session expired. Run `{RELOGIN_COMMAND}`.") -> None:
        super().__init__(message)


class APIError(PushPressError):
    pass


class EndpointsNotConfiguredError(PushPressError):
    def __init__(self, what: str) -> None:
        super().__init__(
            f"The {what} endpoint isn't configured yet. Finish Phase 0 discovery "
            "(docs/endpoints.md) and fill in src/pushpress_mcp/endpoints.py."
        )
