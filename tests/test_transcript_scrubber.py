from __future__ import annotations

import json
from pathlib import Path

import downloader
import transcript_scrubber


def test_scrub_text_removes_timestamp_only_lines_and_joins_spoken_text() -> None:
    raw = "00:01\nWelcome to\nthe show.\n01:10\n>> Thanks for having me.\n"

    assert transcript_scrubber.scrub_text(raw) == "Welcome to the show.\n>> Thanks for having me.\n"


def test_scrub_text_removes_line_leading_timestamp_before_spoken_text() -> None:
    raw = "00:01 First thought\ncontinued here.\n1:02:03 >> Second thought\n"

    assert transcript_scrubber.scrub_text(raw) == "First thought continued here.\n>> Second thought\n"


def test_scrub_text_preserves_timestamp_free_text_and_speaker_markers() -> None:
    raw = "  >> Speaker one\n\nHas   extra\tspace at 00:45.  \n"

    assert transcript_scrubber.scrub_text(raw) == ">> Speaker one Has extra space at 00:45.\n"


def test_scrub_text_is_idempotent() -> None:
    raw = "00:01\n>> A line\n00:02 Next line\n"
    scrubbed = transcript_scrubber.scrub_text(raw)

    assert transcript_scrubber.scrub_text(scrubbed) == scrubbed


def test_run_scrubber_writes_separate_output_and_advances_queue(tmp_path: Path) -> None:
    config = {
        "version": 1,
        "paths": {"transcripts_dir": "transcripts", "browser_profile_dir": ".profile"},
        "downloader": {
            "headless": False,
            "page_timeout_seconds": 120,
            "request_pause_seconds": 3,
            "max_new_episodes_per_show": 1,
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
    raw_path = tmp_path / "transcripts" / "raw" / "episode.txt"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text("00:01\n>> Hello\n00:02 goodbye\n", encoding="utf-8")
    episode = downloader.new_episode_record(
        config["shows"][0], {"video_id": "video__1234", "title": "Title"}, "2026-07-22T00:00:00Z"
    )
    episode["download"]["status"] = "succeeded"
    # This models a queue written before the raw/scrubbed directory split.
    episode["raw_transcript_path"] = "transcripts/episode.txt"
    episode["scrub"] = downloader.stage_state("pending")
    queue = {"version": 1, "shows": {"all-in": {}}, "episodes": {"episode": episode}}
    queue_path = tmp_path / "queue.json"

    report = transcript_scrubber.run_scrubber(config, queue, queue_path, tmp_path)

    scrubbed_path = tmp_path / "transcripts" / "scrubbed" / "episode.txt"
    assert report.scrubbed == 1
    assert scrubbed_path.read_text(encoding="utf-8") == ">> Hello goodbye\n"
    assert episode["scrubbed_transcript_path"] == "transcripts/scrubbed/episode.txt"
    assert episode["scrub"]["status"] == "succeeded"
    assert episode["scrub"]["attempt_count"] == 1
    assert episode["summary"]["status"] == "pending"
    assert json.loads(queue_path.read_text(encoding="utf-8"))["episodes"]["episode"]["scrub"]["status"] == "succeeded"


def test_failed_scrub_remains_retryable(tmp_path: Path) -> None:
    episode = {
        "source_episode_id": "video__1234",
        "download": {"status": "succeeded"},
        "scrub": downloader.stage_state("pending"),
        "raw_transcript_path": "transcripts/raw/missing.txt",
        "summary": downloader.stage_state("not_ready"),
    }
    config = {"paths": {"transcripts_dir": "transcripts"}}
    queue = {"episodes": {"episode": episode}}

    report = transcript_scrubber.run_scrubber(config, queue, tmp_path / "queue.json", tmp_path)

    assert report.failed_scrubs == 1
    assert episode["scrub"]["status"] == "failed"
    assert episode["scrub"]["attempt_count"] == 1
    assert transcript_scrubber.pending_scrubs(queue) == [episode]


def test_force_includes_successful_scrubs() -> None:
    episode = {
        "download": {"status": "succeeded"},
        "scrub": downloader.stage_state("succeeded"),
    }
    queue = {"episodes": {"episode": episode}}

    assert transcript_scrubber.pending_scrubs(queue) == []
    assert transcript_scrubber.pending_scrubs(queue, force=True) == [episode]
