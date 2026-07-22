from __future__ import annotations

import json
from pathlib import Path

import pytest

import downloader


def channel_html(*videos: tuple[str, str]) -> str:
    contents = [
        {
            "richItemRenderer": {
                "content": {
                    "videoRenderer": {
                        "videoId": video_id,
                        "title": {"runs": [{"text": title}]},
                    }
                }
            }
        }
        for video_id, title in videos
    ]
    return f"<script>var ytInitialData = {json.dumps({'contents': {'richGridRenderer': {'contents': contents}}})};</script>"


def video_html(title: str, published_at: str) -> str:
    data = {
        "videoDetails": {"title": title},
        "microformat": {"playerMicroformatRenderer": {"publishDate": published_at}},
    }
    return f"<script>var ytInitialPlayerResponse = {json.dumps(data)};</script>"


def lockup_channel_html(*videos: tuple[str, str]) -> str:
    contents = [
        {
            "richItemRenderer": {
                "content": {
                    "lockupViewModel": {
                        "contentId": video_id,
                        "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                        "metadata": {
                            "lockupMetadataViewModel": {"title": {"content": title}}
                        },
                    }
                }
            }
        }
        for video_id, title in videos
    ]
    return f"<script>var ytInitialData = {json.dumps({'contents': {'richGridRenderer': {'contents': contents}}})};</script>"


def sample_config() -> dict:
    return {
        "version": 1,
        "paths": {
            "transcripts_dir": "transcripts",
            "browser_profile_dir": ".playwright-profile/youtube-transcript",
        },
        "downloader": {
            "headless": False,
            "page_timeout_seconds": 120,
            "request_pause_seconds": 10,
            "max_new_episodes_per_show": 3,
        },
        "shows": [
            {
                "id": "all-in",
                "display_name": "All-In",
                "youtube_videos_url": "https://www.youtube.com/@allin/videos",
                "enabled": True,
            }
        ],
    }


def test_extract_youtube_videos_preserves_channel_order_and_titles() -> None:
    html = channel_html(
        ("newest12345", "Newest &amp; Greatest"),
        ("older123456", "Older episode"),
    )

    assert downloader.extract_youtube_videos(html) == [
        {"video_id": "newest12345", "title": "Newest & Greatest"},
        {"video_id": "older123456", "title": "Older episode"},
    ]


def test_extract_youtube_videos_supports_current_lockup_renderer() -> None:
    html = lockup_channel_html(
        ("newest12345", "Newest episode"),
        ("older123456", "Older episode"),
    )

    assert downloader.extract_youtube_videos(html) == [
        {"video_id": "newest12345", "title": "Newest episode"},
        {"video_id": "older123456", "title": "Older episode"},
    ]


def test_extract_youtube_metadata_normalizes_a_timestamp_to_a_date() -> None:
    assert downloader.extract_youtube_metadata(
        video_html("Episode title", "2026-07-17T06:59:15-07:00")
    ) == {"title": "Episode title", "published_at": "2026-07-17"}


def test_first_discovery_is_limited_to_configured_backlog() -> None:
    config = sample_config()
    queue = downloader.empty_queue(config["shows"])
    added = downloader.discover_show(
        config["shows"][0],
        queue,
        channel_html(
            ("newest12345", "Newest"),
            ("second12345", "Second"),
            ("third_12345", "Third"),
            ("fourth12345", "Fourth"),
        ),
        max_new_episodes=3,
        checked_at="2026-07-22T00:00:00Z",
    )

    assert added == 3
    assert list(queue["episodes"]) == [
        "youtube:all-in:newest12345",
        "youtube:all-in:second12345",
        "youtube:all-in:third_12345",
    ]
    assert queue["shows"]["all-in"]["last_successful_check_at"] == "2026-07-22T00:00:00Z"


def test_later_discovery_stops_at_first_known_video_without_limit() -> None:
    config = sample_config()
    queue = downloader.empty_queue(config["shows"])
    known_video = {"video_id": "known__1234", "title": "Known"}
    queue["episodes"]["youtube:all-in:known__1234"] = downloader.new_episode_record(
        config["shows"][0], known_video, "2026-07-20T00:00:00Z"
    )

    added = downloader.discover_show(
        config["shows"][0],
        queue,
        channel_html(
            ("newest12345", "Newest"),
            ("second12345", "Second"),
            ("known__1234", "Known"),
            ("oldest12345", "Old"),
        ),
        max_new_episodes=1,
        checked_at="2026-07-22T00:00:00Z",
    )

    assert added == 2
    assert "youtube:all-in:oldest12345" not in queue["episodes"]


def test_discover_only_writes_queue_without_calling_downloader(tmp_path: Path) -> None:
    config = sample_config()
    queue = downloader.empty_queue(config["shows"])
    queue_path = tmp_path / "queue.json"

    def no_downloader(*_args: object) -> None:
        raise AssertionError("Browser downloader must not run with --discover-only")

    report = downloader.run_downloader(
        config,
        queue,
        queue_path,
        tmp_path,
        discover_only=True,
        fetcher=lambda _url: channel_html(("newest12345", "Newest")),
        downloader=no_downloader,
    )

    persisted = json.loads(queue_path.read_text())
    assert report.discovered == 1
    assert report.downloaded == 0
    assert "youtube:all-in:newest12345" in persisted["episodes"]


def test_successful_download_updates_stage(tmp_path: Path) -> None:
    config = sample_config()
    queue = downloader.empty_queue(config["shows"])
    episode = downloader.new_episode_record(
        config["shows"][0], {"video_id": "video__1234", "title": "Initial title"}, "2026-07-22T00:00:00Z"
    )
    queue["episodes"]["youtube:all-in:video__1234"] = episode
    queue_path = tmp_path / "queue.json"
    calls: list[Path] = []

    def fake_fetcher(url: str) -> str:
        if url.endswith("/videos"):
            return channel_html(("video__1234", "Initial title"))
        return video_html("Final title", "2026-07-17")

    def fake_downloader(_episode: dict, destination: Path, *_args: object) -> None:
        calls.append(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(">> Speaker\nTranscript", encoding="utf-8")

    report = downloader.run_downloader(
        config,
        queue,
        queue_path,
        tmp_path,
        discover_only=False,
        fetcher=fake_fetcher,
        downloader=fake_downloader,
    )

    assert report.downloaded == 1
    assert episode["download"]["status"] == "succeeded"
    assert episode["summary"]["status"] == "pending"
    assert calls[0].name == "20260717-all-in-video__1234-final-title.txt"


def test_pause_between_download_attempts_is_randomized(tmp_path: Path) -> None:
    config = sample_config()
    queue = downloader.empty_queue(config["shows"])
    for video_id in ("first__1234", "second_1234"):
        queue["episodes"][f"youtube:all-in:{video_id}"] = downloader.new_episode_record(
            config["shows"][0], {"video_id": video_id, "title": "Title"}, "2026-07-22T00:00:00Z"
        )
    pauses: list[float] = []

    def fake_fetcher(url: str) -> str:
        if url.endswith("/videos"):
            return channel_html(("first__1234", "Title"))
        return video_html("Title", "2026-07-17")

    def fake_downloader(_episode: dict, destination: Path, *_args: object) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("Transcript", encoding="utf-8")

    report = downloader.run_downloader(
        config,
        queue,
        tmp_path / "queue.json",
        tmp_path,
        discover_only=False,
        fetcher=fake_fetcher,
        downloader=fake_downloader,
        sleeper=pauses.append,
        random_delay=lambda lower, upper: 6.5,
    )

    assert report.downloaded == 2
    assert pauses == [6.5]


def test_default_downloader_reuses_one_browser_session_for_the_batch(tmp_path: Path) -> None:
    config = sample_config()
    queue = downloader.empty_queue(config["shows"])
    for video_id in ("first__1234", "second_1234"):
        queue["episodes"][f"youtube:all-in:{video_id}"] = downloader.new_episode_record(
            config["shows"][0], {"video_id": video_id, "title": "Title"}, "2026-07-22T00:00:00Z"
        )

    class FakeSession:
        def __init__(self) -> None:
            self.open_count = 0
            self.close_count = 0
            self.downloaded_ids: list[str] = []

        def open(self) -> None:
            self.open_count += 1

        def close(self) -> None:
            self.close_count += 1

        def restart(self) -> None:
            raise AssertionError("A healthy browser session must not restart")

        def download(self, episode: dict, destination: Path) -> None:
            self.downloaded_ids.append(episode["source_episode_id"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("Transcript", encoding="utf-8")

    sessions: list[FakeSession] = []

    def fake_session_factory(*_args: object) -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    def fake_fetcher(url: str) -> str:
        if url.endswith("/videos"):
            return channel_html(("first__1234", "Title"))
        return video_html("Title", "2026-07-17")

    report = downloader.run_downloader(
        config,
        queue,
        tmp_path / "queue.json",
        tmp_path,
        discover_only=False,
        fetcher=fake_fetcher,
        browser_session_factory=fake_session_factory,
    )

    assert report.downloaded == 2
    assert len(sessions) == 1
    assert sessions[0].open_count == 1
    assert sessions[0].downloaded_ids == ["first__1234", "second_1234"]
    assert sessions[0].close_count == 1


def test_browser_context_failure_restarts_session_and_continues(tmp_path: Path) -> None:
    config = sample_config()
    queue = downloader.empty_queue(config["shows"])
    for video_id in ("first__1234", "second_1234"):
        queue["episodes"][f"youtube:all-in:{video_id}"] = downloader.new_episode_record(
            config["shows"][0], {"video_id": video_id, "title": "Title"}, "2026-07-22T00:00:00Z"
        )

    class RecoveringSession:
        def __init__(self) -> None:
            self.restart_count = 0
            self.calls = 0
            self.closed = False

        def open(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

        def restart(self) -> None:
            self.restart_count += 1

        def download(self, _episode: dict, destination: Path) -> None:
            self.calls += 1
            if self.calls == 1:
                raise downloader.BrowserContextFailure("Target closed")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("Transcript", encoding="utf-8")

    session = RecoveringSession()

    def fake_fetcher(url: str) -> str:
        if url.endswith("/videos"):
            return channel_html(("first__1234", "Title"))
        return video_html("Title", "2026-07-17")

    report = downloader.run_downloader(
        config,
        queue,
        tmp_path / "queue.json",
        tmp_path,
        discover_only=False,
        fetcher=fake_fetcher,
        browser_session_factory=lambda *_args: session,
    )

    assert report.failed_downloads == 1
    assert report.downloaded == 1
    assert session.restart_count == 1
    assert session.closed is True


def test_failed_download_is_kept_for_retry(tmp_path: Path) -> None:
    config = sample_config()
    queue = downloader.empty_queue(config["shows"])
    episode = downloader.new_episode_record(
        config["shows"][0], {"video_id": "video__1234", "title": "Title"}, "2026-07-22T00:00:00Z"
    )
    queue["episodes"]["youtube:all-in:video__1234"] = episode

    def failing_downloader(*_args: object) -> None:
        raise RuntimeError("provider unavailable")

    report = downloader.run_downloader(
        config,
        queue,
        tmp_path / "queue.json",
        tmp_path,
        discover_only=False,
        fetcher=lambda url: channel_html(("video__1234", "Title")) if url.endswith("/videos") else video_html("Title", "2026-07-17"),
        downloader=failing_downloader,
    )

    assert report.failed_downloads == 1
    assert episode["download"]["status"] == "failed"
    assert episode["download"]["attempt_count"] == 1
    assert episode["download"]["last_error"] == "provider unavailable"
    assert downloader.pending_downloads(queue) == [episode]


def test_load_config_rejects_pause_shorter_than_three_seconds(tmp_path: Path) -> None:
    config = sample_config()
    config["downloader"]["request_pause_seconds"] = 2
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(downloader.ConfigurationError, match="at least 3"):
        downloader.load_config(path)
