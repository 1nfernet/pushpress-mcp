"""Phase 0: record the members app's network traffic while you click around.

Opens a headed Chromium at the PushPress member app. Log in manually (including
2FA), then visit:
  - the Workouts tab: today, a past week, a future week
  - your results / history
  - a workout's leaderboard
Come back to this terminal and press Enter. Traffic is saved to
discovery/session.har (gitignored; it contains cookies and tokens, never commit
or share it). Then run scripts/summarize_har.py.

Usage: uv run python scripts/discover.py
"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

MEMBERS_URL = "https://members.pushpress.com"
ROOT = Path(__file__).resolve().parent.parent
HAR_PATH = ROOT / "discovery" / "session.har"


def main() -> None:
    HAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            record_har_path=str(HAR_PATH),
            record_har_content="embed",
        )
        page = context.new_page()
        page.goto(MEMBERS_URL)
        print(
            "\nLog in in the browser window, then click through:\n"
            "  1. Workouts: today, a past week, a future week\n"
            "  2. Your results / score history\n"
            "  3. A workout's leaderboard\n"
        )
        input("Press Enter here when done to save the HAR... ")
        context.close()  # HAR is written on close
        browser.close()
    os.chmod(HAR_PATH, 0o600)
    print(f"Saved {HAR_PATH.relative_to(ROOT)}. Next: uv run python scripts/summarize_har.py")


if __name__ == "__main__":
    main()
