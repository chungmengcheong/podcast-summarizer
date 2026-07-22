#!/usr/bin/env python3
"""Check normal browser access to a YouTube Transcript result page.

This is a diagnostic, not a bypass. It reports whether the site exposes an 
enabled ``Copy Transcript`` button for a given YouTube video ID. It uses a 
dedicated persistent Chromium profile, which is the normal (non-private) 
browser mode requested by the site. It does not create an account or alter 
browser fingerprints. 

Install the one optional dependency before running:
    python3 -m pip install playwright
    python3 -m playwright install chromium

Example:
    python3 tests/check_youtube_transcript_access.py PHL1j2ti420
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check ordinary access to a YouTube Transcript result page."
    )
    parser.add_argument("video_id", help="The YouTube video ID (the value of ?v=).")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without a visible browser window. The default is a visible browser.",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(".playwright-profile/youtube-transcript"),
        help=(
            "Dedicated persistent browser-profile directory "
            "(default: .playwright-profile/youtube-transcript)."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="Maximum time to wait for the transcript result or human intervention (default: 120).",
    )
    parser.add_argument(
        "--exercise-controls",
        action="store_true",
        help="After access succeeds, test Copy Transcript and the default .txt download.",
    )
    return parser.parse_args()


def wait_for_transcript_access(page, timeout_seconds: int):
    """Reach enabled transcript controls, allowing an approved anonymous bootstrap."""
    copy_button = page.get_by_role("button", name="Copy Transcript", exact=True)
    anonymous_login = page.get_by_role(
        "button", name="Login Anonymously", exact=True
    )
    deadline = time.monotonic() + timeout_seconds
    anonymous_login_attempted = False
    waiting_for_human = False

    while time.monotonic() < deadline:
        if copy_button.count() == 1 and copy_button.is_enabled():
            return copy_button

        if anonymous_login.count() == 1:
            if not anonymous_login_attempted:
                anonymous_login_attempted = True
                try:
                    anonymous_login.click(timeout=3_000)
                    print("Activated Login Anonymously; waiting for transcript access.")
                except PlaywrightError:
                    print("Could not activate Login Anonymously automatically.")

            if not waiting_for_human:
                waiting_for_human = True
                print(
                    "WAITING: complete any remaining service prompt in the visible browser window."
                )

        page.wait_for_timeout(500)

    return None


def exercise_copy_and_download(page, context, copy_button) -> bool:
    """Verify the enabled Copy and default Download controls without retaining content."""
    context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin="https://www.youtube-transcript.io",
    )
    copy_button.click()
    copied_text = page.evaluate("navigator.clipboard.readText()")
    if not copied_text.strip():
        print("COPY FAILED: the clipboard was empty after Copy Transcript.")
        return False

    print(
        "COPY OK: "
        f"{len(copied_text)} characters; speaker markers present: {'>>' in copied_text}."
    )

    menu_button = page.locator('button[aria-haspopup="menu"]')
    if menu_button.count() != 1:
        print("DOWNLOAD FAILED: could not identify the transcript action menu.")
        return False

    menu_button.click()
    download_menu_item = page.get_by_role("menuitem", name="Download", exact=True)
    if download_menu_item.count() != 1:
        print("DOWNLOAD FAILED: the Download menu item was not available.")
        return False

    download_menu_item.click()
    dialog = page.locator('[role="dialog"]')
    if dialog.count() != 1:
        print("DOWNLOAD FAILED: the download options dialog did not open.")
        return False

    # The site's current modal contains an invalid nested button. The inner button
    # is the unique actionable Download Transcript control.
    download_action = dialog.locator('button:not(:has(button))').filter(
        has_text="Download Transcript"
    )
    if download_action.count() != 1:
        print("DOWNLOAD FAILED: could not identify the final download control.")
        return False

    with page.expect_download(timeout=15_000) as download_info:
        download_action.click()
    download = download_info.value

    with tempfile.TemporaryDirectory() as temporary_directory:
        destination = Path(temporary_directory) / download.suggested_filename
        download.save_as(destination)
        downloaded_text = destination.read_text(encoding="utf-8")

    if not downloaded_text.strip():
        print("DOWNLOAD FAILED: the downloaded .txt file was empty.")
        return False

    print(
        "DOWNLOAD OK: "
        f"{len(downloaded_text)} characters; speaker markers present: {'>>' in downloaded_text}."
    )
    return True


def main() -> int:
    args = parse_args()
    url = f"https://www.youtube-transcript.io/videos?id={args.video_id}"

    with sync_playwright() as playwright:
        # chromium.launch() creates an ephemeral context that sites may identify
        # as private browsing. A persistent context is an ordinary browser profile.
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile_dir.resolve()),
            headless=args.headless,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=args.timeout_seconds * 1000)
            copy_button = wait_for_transcript_access(page, args.timeout_seconds)
            if copy_button is None:
                print("TIMEOUT: no enabled transcript control appeared.")
                return 3

            print(f"AVAILABLE: transcript is accessible at {url}")
            if args.exercise_controls and not exercise_copy_and_download(
                page, context, copy_button
            ):
                return 4

            return 0
        except PlaywrightTimeoutError:
            print(f"TIMEOUT: the result page did not load within {args.timeout_seconds} seconds.")
            return 3
        finally:
            context.close()


if __name__ == "__main__":
    sys.exit(main())
