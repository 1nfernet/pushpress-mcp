"""MCP entrypoint (stdio). Run with `uv run pushpress-mcp`."""

from __future__ import annotations

import logging
import sys
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .store import Store
from .tools import DEFAULT_SCORES_LIMIT, Tools

# stdout carries the MCP protocol; logs go to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx INFO logs full URLs

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

server = MCPServer(
    "pushpress",
    instructions=(
        "Read-only access to the user's own PushPress member data: programmed workouts, "
        "their logged scores, and per-workout leaderboards. Dates are YYYY-MM-DD. "
        "If a response has source=cache and a warning, tell the user the data may be stale "
        "and pass on the warning (e.g. the re-login command)."
    ),
)

_tools: Tools | None = None


def call(method: Callable[[Tools], Any]) -> Any:
    global _tools
    if _tools is None:
        _tools = Tools(Store())
    try:
        return method(_tools)
    except ValueError as e:
        # Bad arguments: show the message. Other exceptions are masked by MCPServer.
        raise ToolError(str(e)) from e


@server.tool(annotations=READ_ONLY)
def get_workouts(start_date: str, end_date: str) -> dict[str, Any]:
    """Programmed workouts between start_date and end_date (YYYY-MM-DD, inclusive). Works for past and future dates."""
    return call(lambda t: t.get_workouts(start_date, end_date))


@server.tool(annotations=READ_ONLY)
def get_my_scores(
    start_date: str | None = None, end_date: str | None = None, limit: int | None = DEFAULT_SCORES_LIMIT
) -> dict[str, Any]:
    """My logged scores, newest first. Optional YYYY-MM-DD date bounds; limit defaults to 50."""
    return call(lambda t: t.get_my_scores(start_date, end_date, limit))


@server.tool(annotations=READ_ONLY)
def get_leaderboard(workout_id: str | None = None, date: str | None = None, title: str | None = None) -> dict[str, Any]:
    """Leaderboard for one workout. Pass workout_id, or date (YYYY-MM-DD) plus optional title text to pick the workout."""
    return call(lambda t: t.get_leaderboard(workout_id, date, title))


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def sync_cache(days_back: int = 365, days_forward: int = 14) -> dict[str, Any]:
    """Pull workouts and my scores into the local cache (never writes to PushPress). Slow: ~1 request/second."""
    return call(lambda t: t.sync_cache(days_back, days_forward))


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
def export_scores_csv(path: str) -> dict[str, Any]:
    """Write all cached scores to a .csv at path, in PushPress's 5-column export format. Overwrites the file."""
    return call(lambda t: t.export_scores_csv(path))


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
