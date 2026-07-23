#!/usr/bin/env python3
"""Generate evidence-aware Markdown summaries from scrubbed podcast transcripts."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from downloader import ConfigurationError, load_config, load_queue, stage_state, utc_now, write_json_atomically
from transcript_scrubber import write_text_atomically


REQUIRED_HEADINGS = (
    "## Executive takeaway",
    "## Key points",
    "## Hot takes",
)
KEY_POINT_HEADING = re.compile(r"^### \d+\.\s+\S")
SummaryRunner = Callable[[str, dict[str, Any], Path], str]


class SummarizerError(RuntimeError):
    """Raised when an episode cannot be summarized safely."""


@dataclass
class SummaryReport:
    summarized: int = 0
    failed_summaries: int = 0
    failures: list[str] = field(default_factory=list)


def load_summarizer_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate the summarizer-specific portion of the user configuration."""
    paths = config.get("paths")
    if not isinstance(paths, dict) or not isinstance(paths.get("summaries_dir"), str):
        raise ConfigurationError("config.paths must define summaries_dir.")

    summarizer = config.get("summarizer")
    if not isinstance(summarizer, dict):
        raise ConfigurationError("config.summarizer must be an object.")
    if summarizer.get("provider") != "codex":
        raise ConfigurationError("config.summarizer.provider must be 'codex' for v1.")
    executable = summarizer.get("executable")
    if not isinstance(executable, str) or not executable.strip():
        raise ConfigurationError("config.summarizer.executable must be a non-empty command or path.")

    model = summarizer.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ConfigurationError("config.summarizer.model must be a non-empty string or null.")
    for field_name in ("timeout_seconds", "max_input_characters"):
        value = summarizer.get(field_name)
        if not isinstance(value, int) or value <= 0:
            raise ConfigurationError(f"config.summarizer.{field_name} must be a positive integer.")
    prompt_path = summarizer.get("prompt_path")
    if not isinstance(prompt_path, str) or not prompt_path.strip():
        raise ConfigurationError("config.summarizer.prompt_path must be a non-empty string.")
    return summarizer


def pending_summaries(
    queue: dict[str, Any],
    force: bool = False,
    source_episode_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return summaries that can be created without downloading or scrubbing again."""
    eligible_statuses = {"pending", "failed"}
    if force:
        eligible_statuses.add("succeeded")
    return [
        episode
        for episode in queue["episodes"].values()
        if episode.get("scrub", {}).get("status") == "succeeded"
        and episode.get("summary", {}).get("status") in eligible_statuses
        and (
            source_episode_ids is None
            or episode.get("source_episode_id") in source_episode_ids
        )
    ]


def episode_metadata(config: dict[str, Any], episode: dict[str, Any]) -> dict[str, str]:
    """Return authoritative output metadata from the queue and user config."""
    show_id = episode.get("show_id")
    show = next((item for item in config["shows"] if item["id"] == show_id), None)
    if show is None:
        raise SummarizerError(f"Episode has an unknown show_id: {show_id!r}.")

    values = {
        "show": show["display_name"],
        "title": episode.get("title"),
        "published_at": episode.get("published_at") or "Unknown",
        "source_url": episode.get("source_url"),
        "transcript_source": episode.get("source"),
    }
    missing = [name for name, value in values.items() if not isinstance(value, str) or not value.strip()]
    if missing:
        raise SummarizerError(f"Episode is missing required metadata: {', '.join(missing)}.")
    return values


def build_prompt(prompt_template: str, metadata: dict[str, str], transcript: str) -> str:
    """Append authoritative metadata and untrusted transcript data to the prompt."""
    metadata_block = "\n".join(
        (
            "<episode_metadata>",
            f"show: {metadata['show']}",
            f"title: {metadata['title']}",
            f"published_at: {metadata['published_at']}",
            f"source_url: {metadata['source_url']}",
            f"transcript_source: {metadata['transcript_source']}",
            "</episode_metadata>",
        )
    )
    return f"{prompt_template.rstrip()}\n\n{metadata_block}\n\n<transcript>\n{transcript.rstrip()}\n</transcript>\n"


def prompt_fingerprint(prompt_template: str) -> str:
    return hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()


def validate_summary(markdown: str) -> str | None:
    """Return a concise validation error, or ``None`` for a contract-compliant summary."""
    if not markdown.strip():
        return "Summary was empty."

    lines = markdown.splitlines()
    if not lines or not re.fullmatch(r"#\s+\S.*", lines[0]):
        return "Summary must start with a single '# <show>: <title>' heading."

    heading_positions: list[int] = []
    for heading in REQUIRED_HEADINGS:
        try:
            heading_positions.append(lines.index(heading))
        except ValueError:
            return f"Summary is missing required heading: {heading}."
    if heading_positions != sorted(heading_positions):
        return "Required summary headings are out of order."

    takeaway_start, key_points_start, hot_takes_start = heading_positions
    key_point_lines = lines[key_points_start + 1 : hot_takes_start]
    if not any(KEY_POINT_HEADING.match(line) for line in key_point_lines):
        return "Summary must contain at least one numbered key point."
    return None


def run_codex(prompt: str, summarizer_config: dict[str, Any], working_directory: Path) -> str:
    """Run Codex in a non-interactive, read-only session and return its final message."""
    with tempfile.TemporaryDirectory(prefix=".summary-codex-", dir=working_directory) as temporary_directory:
        output_path = Path(temporary_directory) / "summary.md"
        command = [
            summarizer_config["executable"],
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--output-last-message",
            str(output_path),
        ]
        model = summarizer_config.get("model")
        if isinstance(model, str) and model:
            command.extend(("--model", model))
        command.append("-")
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                cwd=working_directory,
                timeout=summarizer_config["timeout_seconds"],
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise SummarizerError(
                f"Codex exceeded the {summarizer_config['timeout_seconds']}-second timeout."
            ) from error
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic output"
            raise SummarizerError(f"Codex exited with status {completed.returncode}: {details}")
        if not output_path.is_file():
            raise SummarizerError("Codex completed without writing a final response.")
        return output_path.read_text(encoding="utf-8")


def scrubbed_path_for_episode(config_root: Path, episode: dict[str, Any]) -> Path:
    path_value = episode.get("scrubbed_transcript_path")
    if not isinstance(path_value, str) or not path_value:
        raise SummarizerError("Episode has no scrubbed transcript path.")
    path = config_root / path_value
    if not path.is_file():
        raise SummarizerError(f"Scrubbed transcript does not exist: {path_value}")
    return path


def summary_path_for_episode(config_root: Path, config: dict[str, Any], episode: dict[str, Any]) -> Path:
    transcript_path = scrubbed_path_for_episode(config_root, episode)
    return config_root / config["paths"]["summaries_dir"] / f"{transcript_path.stem}-summary.md"


def run_summarizer(
    config: dict[str, Any],
    queue: dict[str, Any],
    queue_path: Path,
    config_root: Path,
    force: bool = False,
    source_episode_ids: set[str] | None = None,
    runner: SummaryRunner = run_codex,
) -> SummaryReport:
    """Summarize all eligible scrubbed transcripts and persist each outcome."""
    summarizer_config = load_summarizer_config(config)
    prompt_path = config_root / summarizer_config["prompt_path"]
    try:
        prompt_template = prompt_path.read_text(encoding="utf-8")
    except OSError as error:
        raise SummarizerError(f"Could not read prompt file {summarizer_config['prompt_path']}: {error}") from error
    if not prompt_template.strip():
        raise SummarizerError("Prompt file was empty.")

    report = SummaryReport()
    for episode in pending_summaries(queue, force=force, source_episode_ids=source_episode_ids):
        summary_state = episode["summary"]
        summary_state["attempt_count"] = summary_state.get("attempt_count", 0) + 1
        summary_state["last_attempt_at"] = utc_now()
        summary_state["last_error"] = None
        write_json_atomically(queue_path, queue)
        try:
            transcript_path = scrubbed_path_for_episode(config_root, episode)
            transcript = transcript_path.read_text(encoding="utf-8")
            maximum = summarizer_config["max_input_characters"]
            if len(transcript) > maximum:
                raise SummarizerError(
                    f"Transcript exceeds max_input_characters ({len(transcript)} > {maximum})."
                )
            metadata = episode_metadata(config, episode)
            output = runner(build_prompt(prompt_template, metadata, transcript), summarizer_config, config_root)
            validation_error = validate_summary(output)
            if validation_error:
                raise SummarizerError(validation_error)

            destination = summary_path_for_episode(config_root, config, episode)
            write_text_atomically(destination, output.rstrip() + "\n")
            episode["summary"] = stage_state("succeeded") | {
                "attempt_count": summary_state["attempt_count"],
                "last_attempt_at": summary_state["last_attempt_at"],
                "completed_at": utc_now(),
            }
            episode["summary_path"] = str(destination.relative_to(config_root))
            episode["summary_provider"] = summarizer_config["provider"]
            if summarizer_config.get("model") is None:
                episode.pop("summary_model", None)
            else:
                episode["summary_model"] = summarizer_config["model"]
            episode["summary_prompt_fingerprint"] = prompt_fingerprint(prompt_template)
            report.summarized += 1
        except Exception as error:
            summary_state.update(
                {
                    "status": "failed",
                    "completed_at": None,
                    "last_error": str(error),
                }
            )
            report.failed_summaries += 1
            report.failures.append(f"{episode.get('source_episode_id', 'unknown')}: {error}")
        write_json_atomically(queue_path, queue)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize scrubbed podcast transcripts with Codex.")
    parser.add_argument("--config", type=Path, required=True, help="Path to config.json.")
    parser.add_argument("--queue", type=Path, help="Path to queue.json (default: next to config).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate summaries whose current status is succeeded.",
    )
    parser.add_argument(
        "--episode-id",
        action="append",
        dest="source_episode_ids",
        help="Summarize only this source episode ID; repeat to select several episodes.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    try:
        config = load_config(config_path)
        queue_path = (args.queue or config_path.with_name("queue.json")).resolve()
        queue = load_queue(queue_path, config["shows"])
        report = run_summarizer(
            config,
            queue,
            queue_path,
            config_path.parent,
            force=args.force,
            source_episode_ids=set(args.source_episode_ids) if args.source_episode_ids else None,
        )
    except (ConfigurationError, OSError, SummarizerError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    print(f"Summarized: {report.summarized}")
    print(f"Failed summaries: {report.failed_summaries}")
    for failure in report.failures:
        print(f"Summary failed: {failure}", file=sys.stderr)
    return 1 if report.failed_summaries else 0


if __name__ == "__main__":
    sys.exit(main())
