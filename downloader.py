#!/usr/bin/env python3
"""Discover YouTube podcast episodes and download their transcripts.

The command is deliberately file-first: ``config.json`` holds user choices and
``queue.json`` holds the durable episode registry and work queue. The discovery
and state functions are independent of browser automation so they can be tested
without making live requests.
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.request import Request, urlopen

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


YOUTUBE_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
TRANSCRIPT_SOURCE = "youtube-transcript-ui"


class ConfigurationError(ValueError):
    """Raised when the local configuration cannot be safely used."""


class BrowserContextFailure(RuntimeError):
    """A browser-level failure for which the shared session should be restarted."""


@dataclass
class RunReport:
    discovered: int = 0
    downloaded: int = 0
    failed_downloads: int = 0
    discovery_failures: list[str] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"{path} is not valid JSON: {error}") from error

    if not isinstance(data, dict):
        raise ConfigurationError(f"{path} must contain a JSON object.")
    return data


def write_json_atomically(path: Path, data: dict[str, Any]) -> None:
    """Replace a JSON file only after a complete temporary file is available."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(data, temporary_file, indent=2, ensure_ascii=False)
            temporary_file.write("\n")
            temporary_file.flush()
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def load_config(path: Path) -> dict[str, Any]:
    config = read_json(path)
    if config.get("version") != 1:
        raise ConfigurationError("config.json must declare version 1.")

    paths = config.get("paths")
    if not isinstance(paths, dict) or not all(
        isinstance(paths.get(key), str)
        for key in ("transcripts_dir", "browser_profile_dir")
    ):
        raise ConfigurationError("config.paths must define transcript and browser-profile paths.")

    downloader = config.get("downloader")
    if not isinstance(downloader, dict):
        raise ConfigurationError("config.downloader must be an object.")
    if not isinstance(downloader.get("headless"), bool):
        raise ConfigurationError("downloader.headless must be true or false.")
    for key in ("page_timeout_seconds", "request_pause_seconds", "max_new_episodes_per_show"):
        if not isinstance(downloader.get(key), int):
            raise ConfigurationError(f"downloader.{key} must be an integer.")
    if downloader["page_timeout_seconds"] <= 0:
        raise ConfigurationError("downloader.page_timeout_seconds must be positive.")
    if downloader["request_pause_seconds"] < 3:
        raise ConfigurationError("downloader.request_pause_seconds must be at least 3.")
    if downloader["max_new_episodes_per_show"] <= 0:
        raise ConfigurationError("downloader.max_new_episodes_per_show must be positive.")

    shows = config.get("shows")
    if not isinstance(shows, list) or not shows:
        raise ConfigurationError("config.shows must contain at least one show.")
    show_ids: set[str] = set()
    for show in shows:
        if not isinstance(show, dict):
            raise ConfigurationError("Each show must be an object.")
        show_id = show.get("id")
        if not isinstance(show_id, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", show_id):
            raise ConfigurationError("Each show id must be lowercase and hyphenated.")
        if show_id in show_ids:
            raise ConfigurationError(f"Duplicate show id: {show_id}")
        show_ids.add(show_id)
        if not isinstance(show.get("display_name"), str) or not show["display_name"].strip():
            raise ConfigurationError(f"Show {show_id} needs a display_name.")
        source_url = show.get("youtube_videos_url")
        if not isinstance(source_url, str) or not source_url.startswith("https://www.youtube.com/"):
            raise ConfigurationError(f"Show {show_id} needs an HTTPS YouTube videos URL.")
        if not isinstance(show.get("enabled"), bool):
            raise ConfigurationError(f"Show {show_id} needs an enabled flag.")
    return config


def empty_queue(shows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 1,
        "shows": {
            show["id"]: {"last_successful_check_at": None}
            for show in shows
        },
        "episodes": {},
    }


def load_queue(path: Path, shows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    if not path.exists():
        return empty_queue(shows)
    queue = read_json(path)
    if queue.get("version") != 1:
        raise ConfigurationError("queue.json must declare version 1.")
    if not isinstance(queue.get("shows"), dict) or not isinstance(queue.get("episodes"), dict):
        raise ConfigurationError("queue.json must contain shows and episodes objects.")
    for show in shows:
        queue["shows"].setdefault(show["id"], {"last_successful_check_at": None})
    for episode in queue["episodes"].values():
        if not isinstance(episode, dict):
            raise ConfigurationError("Each queue episode must be an object.")
        ensure_episode_lifecycle(episode)
    return queue


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": YOUTUBE_USER_AGENT})
    with urlopen(request, timeout=30) as response:  # noqa: S310 - URLs come from local config.
        return response.read().decode("utf-8")


def extract_assigned_json(page_html: str, variable_name: str) -> dict[str, Any]:
    """Read a JSON object assigned to a JavaScript variable in YouTube HTML."""
    match = re.search(rf"(?:var\s+)?{re.escape(variable_name)}\s*=\s*", page_html)
    if not match:
        raise ValueError(f"Could not find {variable_name} in the YouTube page.")
    object_start = page_html.find("{", match.end())
    if object_start == -1:
        raise ValueError(f"{variable_name} was not assigned a JSON object.")
    try:
        value, _ = json.JSONDecoder().raw_decode(page_html[object_start:])
    except json.JSONDecodeError as error:
        raise ValueError(f"Could not parse {variable_name}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{variable_name} was not a JSON object.")
    return value


def text_from_renderer(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    simple_text = value.get("simpleText")
    if isinstance(simple_text, str):
        return simple_text
    runs = value.get("runs")
    if isinstance(runs, list):
        text = "".join(run.get("text", "") for run in runs if isinstance(run, dict))
        return text or None
    return None


def walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def extract_youtube_videos(page_html: str) -> list[dict[str, str]]:
    """Extract distinct video IDs and titles in their channel-page order."""
    initial_data = extract_assigned_json(page_html, "ytInitialData")
    videos: list[dict[str, str]] = []
    seen_ids: set[str] = set()

    def add_video(video_id: Any, title: Any) -> None:
        if not isinstance(video_id, str) or video_id in seen_ids:
            return
        if not isinstance(title, str):
            title = "Untitled YouTube video"
        videos.append({"video_id": video_id, "title": html.unescape(title)})
        seen_ids.add(video_id)

    for node in walk_json(initial_data):
        renderer = node.get("videoRenderer") or node.get("gridVideoRenderer")
        if isinstance(renderer, dict):
            add_video(renderer.get("videoId"), text_from_renderer(renderer.get("title")))

        # YouTube's current channel-page design uses this renderer instead of
        # videoRenderer. Its contentId is the video ID and its title is plain text.
        lockup = node.get("lockupViewModel")
        if isinstance(lockup, dict) and lockup.get("contentType") == "LOCKUP_CONTENT_TYPE_VIDEO":
            metadata = lockup.get("metadata", {}).get("lockupMetadataViewModel", {})
            title = metadata.get("title", {}).get("content") if isinstance(metadata, dict) else None
            add_video(lockup.get("contentId"), title)
    if not videos:
        raise ValueError("No video entries were found in the YouTube channel page.")
    return videos


def extract_youtube_metadata(page_html: str) -> dict[str, str | None]:
    """Extract the stable title and ISO publication date from a video page."""
    player_response = extract_assigned_json(page_html, "ytInitialPlayerResponse")
    video_details = player_response.get("videoDetails")
    microformat = player_response.get("microformat", {}).get("playerMicroformatRenderer", {})
    title = video_details.get("title") if isinstance(video_details, dict) else None
    raw_published_at = microformat.get("publishDate") if isinstance(microformat, dict) else None
    published_at_match = (
        re.match(r"\d{4}-\d{2}-\d{2}", raw_published_at)
        if isinstance(raw_published_at, str)
        else None
    )
    return {
        "title": html.unescape(title) if isinstance(title, str) else None,
        "published_at": published_at_match.group(0) if published_at_match else None,
    }


def episode_key(show_id: str, video_id: str) -> str:
    return f"youtube:{show_id}:{video_id}"


def new_episode_record(show: dict[str, Any], video: dict[str, str], discovered_at: str) -> dict[str, Any]:
    return {
        "show_id": show["id"],
        "source": TRANSCRIPT_SOURCE,
        "source_episode_id": video["video_id"],
        "source_url": f"https://www.youtube.com/watch?v={video['video_id']}",
        "title": video["title"],
        "published_at": None,
        "discovered_at": discovered_at,
        "raw_transcript_path": None,
        "scrubbed_transcript_path": None,
        "download": {
            "status": "pending",
            "attempt_count": 0,
            "last_attempt_at": None,
            "completed_at": None,
            "last_error": None,
        },
        "scrub": {
            "status": "not_ready",
            "attempt_count": 0,
            "last_attempt_at": None,
            "completed_at": None,
            "last_error": None,
        },
        "summary": {
            "status": "not_ready",
            "attempt_count": 0,
            "last_attempt_at": None,
            "completed_at": None,
            "last_error": None,
        },
    }


def stage_state(status: str) -> dict[str, Any]:
    """Create the standard queue state for a processing stage."""
    return {
        "status": status,
        "attempt_count": 0,
        "last_attempt_at": None,
        "completed_at": None,
        "last_error": None,
    }


def ensure_episode_lifecycle(episode: dict[str, Any]) -> None:
    """Upgrade pre-scrubber queue records in memory without losing their raw path."""
    if "raw_transcript_path" not in episode:
        episode["raw_transcript_path"] = episode.pop("transcript_path", None)
    episode.setdefault("scrubbed_transcript_path", None)

    download = episode.get("download", {})
    if not isinstance(download, dict):
        raise ConfigurationError("Each episode download state must be an object.")
    if "scrub" not in episode:
        scrub_status = "pending" if download.get("status") == "succeeded" else "not_ready"
        episode["scrub"] = stage_state(scrub_status)
        # Older downloads made a summary ready immediately. It must now wait
        # for the intermediate scrub stage.
        summary = episode.get("summary")
        if isinstance(summary, dict) and summary.get("status") == "pending":
            summary["status"] = "not_ready"
    if not isinstance(episode["scrub"], dict):
        raise ConfigurationError("Each episode scrub state must be an object.")
    if "summary" not in episode:
        summary_status = "pending" if episode["scrub"].get("status") == "succeeded" else "not_ready"
        episode["summary"] = stage_state(summary_status)
    if not isinstance(episode["summary"], dict):
        raise ConfigurationError("Each episode summary state must be an object.")


def discover_show(
    show: dict[str, Any],
    queue: dict[str, Any],
    channel_html: str,
    max_new_episodes: int,
    checked_at: str | None = None,
) -> int:
    """Add new channel entries, stopping at the first known video ID."""
    videos = extract_youtube_videos(channel_html)
    known_ids = {
        episode.get("source_episode_id")
        for episode in queue["episodes"].values()
        if episode.get("show_id") == show["id"]
    }
    first_check = not known_ids
    added = 0
    for video in videos:
        if video["video_id"] in known_ids:
            break
        if first_check and added >= max_new_episodes:
            break
        key = episode_key(show["id"], video["video_id"])
        queue["episodes"][key] = new_episode_record(show, video, checked_at or utc_now())
        added += 1

    queue["shows"].setdefault(show["id"], {})["last_successful_check_at"] = checked_at or utc_now()
    return added


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug[:80].rstrip("-") or "untitled"


def raw_transcript_path_for_episode(transcripts_dir: Path, episode: dict[str, Any]) -> Path:
    published_at = episode.get("published_at")
    if not isinstance(published_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_at):
        raise ValueError("Episode has no ISO publication date.")
    filename = "-".join(
        [
            published_at.replace("-", ""),
            episode["show_id"],
            episode["source_episode_id"],
            slugify(episode["title"]),
        ]
    ) + ".txt"
    return transcripts_dir / "raw" / filename


def wait_for_transcript_access(page: Any, timeout_seconds: int) -> None:
    """Wait for the provider's enabled transcript controls, with a visible fallback."""
    copy_button = page.get_by_role("button", name="Copy Transcript", exact=True)
    anonymous_login = page.get_by_role("button", name="Login Anonymously", exact=True)
    deadline = time.monotonic() + timeout_seconds
    anonymous_login_attempted = False
    waiting_for_human = False

    while time.monotonic() < deadline:
        if copy_button.count() == 1 and copy_button.is_enabled():
            return
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
                print("WAITING: complete any remaining service prompt in the visible browser window.")
        page.wait_for_timeout(500)
    raise TimeoutError("No enabled transcript controls appeared.")


class TranscriptBrowserSession:
    """One persistent browser context, with a fresh page for each episode."""

    def __init__(self, browser_profile_dir: Path, headless: bool, timeout_seconds: int):
        self.browser_profile_dir = browser_profile_dir
        self.headless = headless
        self.timeout_seconds = timeout_seconds
        self._playwright: Any | None = None
        self._context: Any | None = None

    def open(self) -> None:
        if self._context is not None:
            return
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.browser_profile_dir),
            headless=self.headless,
        )

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def restart(self) -> None:
        self.close()
        self.open()

    def download(self, episode: dict[str, Any], output_path: Path) -> None:
        """Download an episode while retaining the shared provider session."""
        self.open()
        assert self._context is not None
        output_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://www.youtube-transcript.io/videos?id={episode['source_episode_id']}"
        page = self._context.new_page()
        temporary_path: Path | None = None
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1000)
            wait_for_transcript_access(page, self.timeout_seconds)

            menu_button = page.locator('button[aria-haspopup="menu"]')
            if menu_button.count() != 1:
                raise RuntimeError("Could not identify the transcript action menu.")
            menu_button.click()
            download_item = page.get_by_role("menuitem", name="Download", exact=True)
            if download_item.count() != 1:
                raise RuntimeError("The transcript Download menu item was not available.")
            download_item.click()

            dialog = page.locator('[role="dialog"]')
            if dialog.count() != 1:
                raise RuntimeError("The transcript download options dialog did not open.")
            download_action = dialog.locator('button:not(:has(button))').filter(
                has_text="Download Transcript"
            )
            if download_action.count() != 1:
                raise RuntimeError("Could not identify the final transcript Download control.")

            with page.expect_download(timeout=self.timeout_seconds * 1000) as download_info:
                download_action.click()
            download = download_info.value
            with tempfile.NamedTemporaryFile(
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
            download.save_as(temporary_path)
            if not temporary_path.read_text(encoding="utf-8").strip():
                raise RuntimeError("The downloaded transcript file was empty.")
            temporary_path.replace(output_path)
            temporary_path = None
        except PlaywrightTimeoutError as error:
            raise RuntimeError(f"Timed out while downloading {episode['source_episode_id']}.") from error
        except PlaywrightError as error:
            message = str(error)
            if any(
                marker in message
                for marker in ("Target page, context or browser has been closed", "Target closed", "Browser has been closed")
            ):
                raise BrowserContextFailure(message) from error
            raise RuntimeError(f"Browser error while downloading {episode['source_episode_id']}: {message}") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            page.close()


def download_transcript(
    episode: dict[str, Any],
    output_path: Path,
    browser_profile_dir: Path,
    headless: bool,
    timeout_seconds: int,
) -> None:
    """Compatibility wrapper for one-off diagnostics and direct callers."""
    session = TranscriptBrowserSession(browser_profile_dir, headless, timeout_seconds)
    try:
        session.download(episode, output_path)
    finally:
        session.close()


def pending_downloads(queue: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        episode
        for episode in queue["episodes"].values()
        if episode.get("download", {}).get("status") in {"pending", "failed"}
    ]


def enrich_episode_metadata(episode: dict[str, Any], fetcher: Callable[[str], str]) -> None:
    if episode.get("published_at") and episode.get("title"):
        return
    metadata = extract_youtube_metadata(fetcher(episode["source_url"]))
    if metadata["title"]:
        episode["title"] = metadata["title"]
    if metadata["published_at"]:
        episode["published_at"] = metadata["published_at"]
    if not episode.get("published_at"):
        raise ValueError("YouTube video page did not supply an ISO publication date.")


def run_downloader(
    config: dict[str, Any],
    queue: dict[str, Any],
    queue_path: Path,
    config_root: Path,
    discover_only: bool,
    fetcher: Callable[[str], str] = fetch_text,
    downloader: Callable[..., None] | None = None,
    browser_session_factory: Callable[[Path, bool, int], Any] = TranscriptBrowserSession,
    sleeper: Callable[[float], None] = time.sleep,
    random_delay: Callable[[float, float], float] = random.uniform,
) -> RunReport:
    report = RunReport()
    settings = config["downloader"]

    for show in config["shows"]:
        if not show["enabled"]:
            continue
        try:
            channel_html = fetcher(show["youtube_videos_url"])
            report.discovered += discover_show(
                show,
                queue,
                channel_html,
                settings["max_new_episodes_per_show"],
            )
            write_json_atomically(queue_path, queue)
        except Exception as error:
            report.discovery_failures.append(f"{show['id']}: {error}")

    if discover_only:
        return report

    transcripts_dir = config_root / config["paths"]["transcripts_dir"]
    browser_profile_dir = config_root / config["paths"]["browser_profile_dir"]
    episodes_to_download = pending_downloads(queue)
    browser_session: Any | None = None
    try:
        for index, episode in enumerate(episodes_to_download):
            download_state = episode["download"]
            download_state["attempt_count"] += 1
            download_state["last_attempt_at"] = utc_now()
            download_state["last_error"] = None
            write_json_atomically(queue_path, queue)
            try:
                enrich_episode_metadata(episode, fetcher)
                destination = raw_transcript_path_for_episode(transcripts_dir, episode)
                if downloader is not None:
                    downloader(
                        episode,
                        destination,
                        browser_profile_dir,
                        settings["headless"],
                        settings["page_timeout_seconds"],
                    )
                else:
                    if browser_session is None:
                        browser_session = browser_session_factory(
                            browser_profile_dir,
                            settings["headless"],
                            settings["page_timeout_seconds"],
                        )
                        browser_session.open()
                    browser_session.download(episode, destination)
                episode["raw_transcript_path"] = str(destination.relative_to(config_root))
                download_state.update(
                    {
                        "status": "succeeded",
                        "completed_at": utc_now(),
                        "last_error": None,
                    }
                )
                episode["scrub"] = stage_state("pending")
                episode["summary"] = stage_state("not_ready")
                report.downloaded += 1
            except Exception as error:
                download_state.update(
                    {
                        "status": "failed",
                        "completed_at": None,
                        "last_error": str(error),
                    }
                )
                report.failed_downloads += 1
                if isinstance(error, BrowserContextFailure) and browser_session is not None:
                    try:
                        browser_session.restart()
                    except Exception as restart_error:
                        download_state["last_error"] += f"; browser restart failed: {restart_error}"
            write_json_atomically(queue_path, queue)

            if index < len(episodes_to_download) - 1:
                sleeper(random_delay(3, settings["request_pause_seconds"]))
    finally:
        if browser_session is not None:
            browser_session.close()
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover and download podcast transcripts.")
    parser.add_argument("--config", type=Path, required=True, help="Path to config.json.")
    parser.add_argument("--queue", type=Path, help="Path to queue.json (default: next to config).")
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Update the queue with newly discovered episodes without opening the browser.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    try:
        config = load_config(config_path)
        queue_path = (args.queue or config_path.with_name("queue.json")).resolve()
        queue = load_queue(queue_path, config["shows"])
        report = run_downloader(
            config,
            queue,
            queue_path,
            config_path.parent,
            args.discover_only,
        )
    except (ConfigurationError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    print(f"Discovered: {report.discovered}")
    if args.discover_only:
        print("Download step skipped (--discover-only).")
    else:
        print(f"Downloaded: {report.downloaded}")
        print(f"Failed downloads: {report.failed_downloads}")
    for failure in report.discovery_failures:
        print(f"Discovery failed: {failure}", file=sys.stderr)
    return 1 if report.discovery_failures or report.failed_downloads else 0


if __name__ == "__main__":
    sys.exit(main())
