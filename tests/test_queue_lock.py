from __future__ import annotations

import io
from pathlib import Path

import poddigest
import pytest
from queue_lock import QueueLock, QueueLockUnavailable, lock_path_for


def test_queue_lock_excludes_a_second_writer_and_releases_after_exit(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"

    with QueueLock(queue_path):
        assert lock_path_for(queue_path).is_file()
        with pytest.raises(QueueLockUnavailable, match="already using queue.json"):
            with QueueLock(queue_path):
                pass

    with QueueLock(queue_path):
        pass


def test_orchestrator_reports_a_busy_queue_without_starting_a_run(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """{
  \"version\": 1,
  \"paths\": {\"transcripts_dir\": \"transcripts\", \"summaries_dir\": \"transcripts/summarized\", \"browser_profile_dir\": \".profile\"},
  \"downloader\": {\"headless\": true, \"page_timeout_seconds\": 120, \"request_pause_seconds\": 3, \"max_new_episodes_per_show\": 1},
  \"summarizer\": {\"provider\": \"codex\", \"executable\": \"codex\", \"model\": null, \"timeout_seconds\": 60, \"max_input_characters\": 100000, \"prompt_path\": \"prompts/summary_prompt.md\"},
  \"delivery\": {\"obsidian_target_dir\": \"/tmp\", \"instapaper_url\": \"https://example.com\"},
  \"shows\": [{\"id\": \"all-in\", \"display_name\": \"All-In\", \"youtube_videos_url\": \"https://www.youtube.com/@allin/videos\", \"enabled\": true}]
}""",
        encoding="utf-8",
    )
    queue_path = tmp_path / "queue.json"
    args = poddigest.parse_args(["--config", str(config_path), "--no-live"])
    output = io.StringIO()
    diagnostics = io.StringIO()

    with QueueLock(queue_path):
        result = poddigest.run_workflow(args, output=output, diagnostics=diagnostics)

    assert result == 2
    assert output.getvalue() == ""
    assert "already using queue.json" in diagnostics.getvalue()
    assert not (tmp_path / "logs").exists()
