from __future__ import annotations

import json
from pathlib import Path

import pytest

import downloader
import delivery


def sample_config(obsidian_target_dir: Path) -> dict:
    return {
        "version": 1,
        "paths": {
            "transcripts_dir": "transcripts",
            "summaries_dir": "transcripts/summarized",
            "browser_profile_dir": ".profile",
        },
        "downloader": {
            "headless": False,
            "page_timeout_seconds": 120,
            "request_pause_seconds": 3,
            "max_new_episodes_per_show": 1,
        },
        "delivery": {
            "obsidian_target_dir": str(obsidian_target_dir),
            "instapaper_url": "http://www.nourl.com",
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


def episode(episode_id: str, published_at: str, summary_path: str) -> dict:
    return {
        "show_id": "all-in",
        "source_episode_id": episode_id,
        "published_at": published_at,
        "summary_path": summary_path,
        "summary": downloader.stage_state("succeeded"),
    }


def write_summary(root: Path, name: str, heading: str) -> str:
    path = root / "transcripts" / "summarized" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {heading}\n\n## Executive takeaway\n\nUseful.\n", encoding="utf-8")
    return str(path.relative_to(root))


def test_pending_deliveries_is_newest_first_and_excludes_delivered() -> None:
    newest = episode("new", "2026-07-17", "new.md")
    oldest = episode("old", "2026-07-15", "old.md")
    delivered = episode("done", "2026-07-20", "done.md")
    delivered["delivery"] = downloader.stage_state("succeeded")
    queue = {"episodes": {"old": oldest, "new": newest, "done": delivered}}

    assert delivery.pending_deliveries(queue) == [newest, oldest]


def test_run_delivery_delivers_one_newest_first_batch_and_marks_queue(tmp_path: Path) -> None:
    obsidian_dir = tmp_path / "obsidian"
    config = sample_config(obsidian_dir)
    oldest = episode("old", "2026-07-15", write_summary(tmp_path, "old.md", "Old summary"))
    newest = episode("new", "2026-07-17", write_summary(tmp_path, "new.md", "New summary"))
    queue = {"version": 1, "shows": {"all-in": {}}, "episodes": {"old": oldest, "new": newest}}
    queue_path = tmp_path / "queue.json"
    (tmp_path / ".env").write_text("INSTAPAPER_USERNAME=user\nINSTAPAPER_PASSWORD=password\n", encoding="utf-8")
    calls: list[tuple[str, str, str, str, str]] = []

    report = delivery.run_delivery(
        config,
        queue,
        queue_path,
        tmp_path,
        instapaper_sender=lambda *args: calls.append(args),
    )

    title = "20260715-20260717-2 summaries"
    expected_path = tmp_path / "transcripts" / "collated" / f"{title}.md"
    content = expected_path.read_text(encoding="utf-8")
    assert report.delivered == 2
    assert report.failed_deliveries == 0
    assert content.startswith(f"# {title}\n\n# New summary")
    assert content.index("# New summary") < content.index("# Old summary")
    assert (obsidian_dir / expected_path.name).read_text(encoding="utf-8") == content
    assert calls == [("user", "password", "http://www.nourl.com", title, "https://www.instapaper.com/api/add")]
    for item in (oldest, newest):
        assert item["delivery"]["status"] == "succeeded"
        assert item["delivery"]["batch_path"] == str(expected_path.relative_to(tmp_path))
    saved_queue = json.loads(queue_path.read_text(encoding="utf-8"))
    assert saved_queue["episodes"]["new"]["delivery"]["status"] == "succeeded"


def test_instapaper_failure_warns_without_undoing_obsidian_delivery(tmp_path: Path) -> None:
    config = sample_config(tmp_path / "obsidian")
    record = episode("episode", "2026-07-17", write_summary(tmp_path, "episode.md", "Summary"))
    queue = {"version": 1, "shows": {"all-in": {}}, "episodes": {"episode": record}}
    queue_path = tmp_path / "queue.json"
    (tmp_path / ".env").write_text("INSTAPAPER_USERNAME=user\nINSTAPAPER_PASSWORD=password\n", encoding="utf-8")

    report = delivery.run_delivery(
        config,
        queue,
        queue_path,
        tmp_path,
        instapaper_sender=lambda *_args: (_ for _ in ()).throw(RuntimeError("network unavailable")),
    )

    assert report.delivered == 1
    assert report.failed_deliveries == 0
    assert report.instapaper_warning == "network unavailable"
    assert record["delivery"]["status"] == "succeeded"
    assert delivery.pending_deliveries(queue) == []


def test_existing_different_obsidian_file_is_a_retryable_delivery_failure(tmp_path: Path) -> None:
    obsidian_dir = tmp_path / "obsidian"
    config = sample_config(obsidian_dir)
    record = episode("episode", "2026-07-17", write_summary(tmp_path, "episode.md", "Summary"))
    queue = {"version": 1, "shows": {"all-in": {}}, "episodes": {"episode": record}}
    queue_path = tmp_path / "queue.json"
    expected_destination = obsidian_dir / "20260717-20260717-1 summaries.md"
    expected_destination.parent.mkdir(parents=True)
    expected_destination.write_text("# A different note\n", encoding="utf-8")

    report = delivery.run_delivery(config, queue, queue_path, tmp_path)

    assert report.delivered == 0
    assert report.failed_deliveries == 1
    assert "different content" in report.failures[0]
    assert record["delivery"]["status"] == "failed"
    assert expected_destination.read_text(encoding="utf-8") == "# A different note\n"


def test_delivery_config_requires_an_absolute_obsidian_path(tmp_path: Path) -> None:
    config = sample_config(tmp_path / "obsidian")
    config["delivery"]["obsidian_target_dir"] = "relative"

    with pytest.raises(downloader.ConfigurationError, match="absolute"):
        delivery.load_delivery_config(config)
