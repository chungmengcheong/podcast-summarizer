#!/usr/bin/env python3
"""Normalize downloaded podcast transcripts without changing their content."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from downloader import (
    ConfigurationError,
    load_config,
    load_queue,
    stage_state,
    utc_now,
    write_json_atomically,
)


TIMESTAMP_PREFIX = re.compile(r"^(?:(?:\d{1,2}:)?\d{1,2}:\d{2})(?=\s|$)\s*")
WHITESPACE = re.compile(r"\s+")
SPEAKER_MARKER = re.compile(r"\s*>>\s*")


@dataclass
class ScrubReport:
    scrubbed: int = 0
    failed_scrubs: int = 0
    failures: list[str] = field(default_factory=list)


def scrub_text(raw_text: str) -> str:
    """Remove line-leading timestamps and normalize whitespace.

    Each ``>>`` marker starts its own line, retaining it as a speaker-turn cue
    while making the turns easy to inspect in the scrubbed transcript.
    """
    without_timestamps = (TIMESTAMP_PREFIX.sub("", line) for line in raw_text.splitlines())
    normalized = WHITESPACE.sub(" ", " ".join(without_timestamps)).strip()
    return SPEAKER_MARKER.sub("\n>> ", normalized).lstrip() + "\n"


def write_text_atomically(path: Path, content: str) -> None:
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
            temporary_file.write(content)
            temporary_file.flush()
        temporary_path.replace(path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def pending_scrubs(queue: dict[str, Any], force: bool = False) -> list[dict[str, Any]]:
    eligible_statuses = {"pending", "failed"}
    if force:
        eligible_statuses.add("succeeded")
    return [
        episode
        for episode in queue["episodes"].values()
        if episode.get("download", {}).get("status") == "succeeded"
        and episode.get("scrub", {}).get("status") in eligible_statuses
    ]


def paths_for_episode(config_root: Path, transcripts_dir: Path, episode: dict[str, Any]) -> tuple[Path, Path]:
    raw_path_value = episode.get("raw_transcript_path")
    if not isinstance(raw_path_value, str) or not raw_path_value:
        raise ValueError("Episode has no raw transcript path.")
    raw_path = config_root / raw_path_value
    if not raw_path.is_file():
        # Queue records created before the raw/scrubbed layout stored the same
        # filename directly in transcripts/. Adopt it if it has since been
        # moved into the new raw directory.
        migrated_raw_path = transcripts_dir / "raw" / raw_path.name
        if migrated_raw_path.is_file():
            raw_path = migrated_raw_path
            episode["raw_transcript_path"] = str(raw_path.relative_to(config_root))
    return raw_path, transcripts_dir / "scrubbed" / raw_path.name


def run_scrubber(
    config: dict[str, Any],
    queue: dict[str, Any],
    queue_path: Path,
    config_root: Path,
    force: bool = False,
) -> ScrubReport:
    """Scrub eligible episodes without re-downloading their raw text."""
    report = ScrubReport()
    transcripts_dir = config_root / config["paths"]["transcripts_dir"]

    for episode in pending_scrubs(queue, force=force):
        scrub_state = episode["scrub"]
        scrub_state["attempt_count"] += 1
        scrub_state["last_attempt_at"] = utc_now()
        scrub_state["last_error"] = None
        write_json_atomically(queue_path, queue)
        try:
            raw_path, scrubbed_path = paths_for_episode(config_root, transcripts_dir, episode)
            write_text_atomically(scrubbed_path, scrub_text(raw_path.read_text(encoding="utf-8")))
            episode["scrubbed_transcript_path"] = str(scrubbed_path.relative_to(config_root))
            episode["scrub"] = stage_state("succeeded") | {
                "attempt_count": scrub_state["attempt_count"],
                "last_attempt_at": scrub_state["last_attempt_at"],
                "completed_at": utc_now(),
            }
            episode["summary"] = stage_state("pending")
            report.scrubbed += 1
        except Exception as error:
            scrub_state.update(
                {
                    "status": "failed",
                    "completed_at": None,
                    "last_error": str(error),
                }
            )
            report.failed_scrubs += 1
            report.failures.append(f"{episode.get('source_episode_id', 'unknown')}: {error}")
        write_json_atomically(queue_path, queue)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrub downloaded podcast transcripts.")
    parser.add_argument("--config", type=Path, required=True, help="Path to config.json.")
    parser.add_argument("--queue", type=Path, help="Path to queue.json (default: next to config).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recreate scrubbed transcripts from their raw inputs, including successful scrubs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    try:
        config = load_config(config_path)
        queue_path = (args.queue or config_path.with_name("queue.json")).resolve()
        queue = load_queue(queue_path, config["shows"])
        report = run_scrubber(config, queue, queue_path, config_path.parent, force=args.force)
    except (ConfigurationError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    print(f"Scrubbed: {report.scrubbed}")
    print(f"Failed scrubs: {report.failed_scrubs}")
    for failure in report.failures:
        print(f"Scrub failed: {failure}", file=sys.stderr)
    return 1 if report.failed_scrubs else 0


if __name__ == "__main__":
    sys.exit(main())
