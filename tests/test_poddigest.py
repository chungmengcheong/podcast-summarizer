from __future__ import annotations

import json
from pathlib import Path

import pytest

import delivery
import downloader
import poddigest
import summarizer
import transcript_scrubber


def write_config(tmp_path: Path) -> Path:
    config = {
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
        "summarizer": {
            "provider": "codex",
            "executable": "codex",
            "model": None,
            "timeout_seconds": 60,
            "max_input_characters": 100_000,
            "prompt_path": "prompts/summary_prompt.md",
        },
        "delivery": {
            "obsidian_target_dir": str(tmp_path / "obsidian"),
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
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_full_run_reports_all_component_outcomes_and_writes_minimal_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = write_config(tmp_path)
    calls: list[str] = []

    def download(*_args: object, **_kwargs: object) -> downloader.RunReport:
        calls.append("download")
        return downloader.RunReport(discovered=2, downloaded=2)

    def scrub(*_args: object, **_kwargs: object) -> transcript_scrubber.ScrubReport:
        calls.append("scrub")
        return transcript_scrubber.ScrubReport(scrubbed=2)

    def summarize(*_args: object, **_kwargs: object) -> summarizer.SummaryReport:
        calls.append("summarize")
        return summarizer.SummaryReport(summarized=2)

    def deliver(*_args: object, **_kwargs: object) -> delivery.DeliveryReport:
        calls.append("delivery")
        return delivery.DeliveryReport(delivered=2)

    monkeypatch.setattr(poddigest.downloader, "run_downloader", download)
    monkeypatch.setattr(poddigest.transcript_scrubber, "run_scrubber", scrub)
    monkeypatch.setattr(poddigest.summarizer, "run_summarizer", summarize)
    monkeypatch.setattr(poddigest.delivery, "run_delivery", deliver)

    assert poddigest.main(["--config", str(config_path), "--no-live"]) == 0
    assert calls == ["download", "scrub", "summarize", "delivery"]
    assert "Download: 2 discovered, 2 downloaded, 0 failed" in capsys.readouterr().out
    log = next((tmp_path / "logs" / "runs").glob("*.log"))
    contents = log.read_text(encoding="utf-8")
    assert "run started" in contents
    assert "download started" in contents
    assert "delivery completed: delivered=2 failed=0 instapaper_warning=false" in contents
    assert "run completed: exit_code=0" in contents


def test_force_requires_a_selected_scrub_or_summarize_stage() -> None:
    with pytest.raises(SystemExit, match="2"):
        poddigest.parse_args(["--force"])
    with pytest.raises(SystemExit, match="2"):
        poddigest.parse_args(["--stage", "delivery", "--force"])
    args = poddigest.parse_args(["--stage", "scrub", "--force"])
    assert args.stage == "scrub"
    assert args.force is True


def test_selected_stage_receives_force_and_does_not_invoke_other_components(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = write_config(tmp_path)
    received_force: list[bool] = []

    def scrub(*_args: object, force: bool, **_kwargs: object) -> transcript_scrubber.ScrubReport:
        received_force.append(force)
        return transcript_scrubber.ScrubReport(scrubbed=1)

    monkeypatch.setattr(poddigest.transcript_scrubber, "run_scrubber", scrub)
    monkeypatch.setattr(
        poddigest.downloader,
        "run_downloader",
        lambda *_args, **_kwargs: pytest.fail("download should not be invoked"),
    )
    monkeypatch.setattr(
        poddigest.summarizer,
        "run_summarizer",
        lambda *_args, **_kwargs: pytest.fail("summarize should not be invoked"),
    )
    monkeypatch.setattr(
        poddigest.delivery,
        "run_delivery",
        lambda *_args, **_kwargs: pytest.fail("delivery should not be invoked"),
    )

    assert poddigest.main(["--config", str(config_path), "--stage", "scrub", "--force", "--no-live"]) == 0
    assert received_force == [True]


def test_discover_only_invokes_only_the_downloader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    discover_only_values: list[bool] = []

    def download(*_args: object, discover_only: bool, **_kwargs: object) -> downloader.RunReport:
        discover_only_values.append(discover_only)
        return downloader.RunReport(discovered=1)

    monkeypatch.setattr(poddigest.downloader, "run_downloader", download)
    monkeypatch.setattr(
        poddigest.transcript_scrubber,
        "run_scrubber",
        lambda *_args, **_kwargs: pytest.fail("scrub should not be invoked"),
    )
    monkeypatch.setattr(
        poddigest.summarizer,
        "run_summarizer",
        lambda *_args, **_kwargs: pytest.fail("summarize should not be invoked"),
    )
    monkeypatch.setattr(
        poddigest.delivery,
        "run_delivery",
        lambda *_args, **_kwargs: pytest.fail("delivery should not be invoked"),
    )

    assert poddigest.main(["--config", str(config_path), "--discover-only", "--no-live"]) == 0
    assert discover_only_values == [True]


def test_failure_does_not_block_delivery_and_returns_retryable_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = write_config(tmp_path)
    delivered: list[bool] = []

    monkeypatch.setattr(
        poddigest.downloader,
        "run_downloader",
        lambda *_args, **_kwargs: downloader.RunReport(downloaded=1, failed_downloads=1),
    )
    monkeypatch.setattr(
        poddigest.transcript_scrubber,
        "run_scrubber",
        lambda *_args, **_kwargs: transcript_scrubber.ScrubReport(scrubbed=1),
    )
    monkeypatch.setattr(
        poddigest.summarizer,
        "run_summarizer",
        lambda *_args, **_kwargs: summarizer.SummaryReport(summarized=1),
    )
    monkeypatch.setattr(
        poddigest.delivery,
        "run_delivery",
        lambda *_args, **_kwargs: delivered.append(True) or delivery.DeliveryReport(delivered=1),
    )

    assert poddigest.main(["--config", str(config_path), "--no-live"]) == 1
    assert delivered == [True]


def test_instapaper_warning_is_visible_but_successful_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = write_config(tmp_path)
    monkeypatch.setattr(
        poddigest.delivery,
        "run_delivery",
        lambda *_args, **_kwargs: delivery.DeliveryReport(delivered=1, instapaper_warning="network unavailable"),
    )

    assert poddigest.main(["--config", str(config_path), "--stage", "delivery", "--no-live"]) == 0
    log = next((tmp_path / "logs" / "runs").glob("*.log"))
    assert "instapaper_warning=true" in log.read_text(encoding="utf-8")
