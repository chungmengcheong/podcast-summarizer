#!/usr/bin/env python3
"""Run the podcast workflow and present one coherent operational report."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

import delivery
import downloader
import summarizer
import transcript_scrubber
from queue_lock import QueueLock, QueueLockUnavailable


STAGES = ("download", "scrub", "summarize", "delivery")
STAGE_LABELS = {
    "download": "Download",
    "scrub": "Scrub",
    "summarize": "Summarize",
    "delivery": "Delivery",
}
SECRET_VALUE = re.compile(
    r"(?i)\b(password|token|secret|authorization|cookie|credential)\b\s*[:=]\s*\S+"
)


def utc_timestamp() -> str:
    """Return an ISO-8601 timestamp suitable for terminal and log output."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log_filename() -> str:
    """Return the sortable name used for a single invocation's run log."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ.log")


def safe_message(error: object) -> str:
    """Render a bounded, single-line diagnostic without obvious secret values."""
    message = " ".join(str(error).split())
    message = SECRET_VALUE.sub(r"\1=[redacted]", message)
    return message[:400] or "no diagnostic message"


class RunLogger:
    """A deliberately small per-run text log."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        filename = log_filename()
        base_stem = Path(filename).stem
        candidate = directory / filename
        suffix = 2
        while candidate.exists():
            candidate = directory / f"{base_stem}-{suffix}.log"
            suffix += 1
        self.path = candidate
        self.path.touch(exist_ok=False)

    def write(self, message: str) -> None:
        with self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{utc_timestamp()} {message}\n")


class ProgressReporter:
    """Render a compact live board, with safe append-only fallback output."""

    def __init__(self, output: TextIO, live: bool):
        self.output = output
        self.live = live
        self._drawn = False
        self.states = {stage: "pending" for stage in STAGES}

    def update(self, stage: str, status: str) -> None:
        self.states[stage] = status
        if self.live:
            self._draw()
            return
        self.output.write(f"{STAGE_LABELS[stage]}: {status}\n")
        self.output.flush()

    def finish(self, outcome: str, log_path: Path) -> None:
        if self.live and self._drawn:
            self.output.write("\n")
        self.output.write(f"Run {outcome}. Log: {log_path}\n")
        self.output.flush()

    def _draw(self) -> None:
        if self._drawn:
            self.output.write(f"\x1b[{len(STAGES)}A")
        for stage in STAGES:
            self.output.write(f"\r\x1b[2K{STAGE_LABELS[stage]}: {self.states[stage]}\n")
        self.output.flush()
        self._drawn = True


@dataclass
class RunState:
    failed: bool = False
    warned: bool = False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the complete podcast summarizer workflow.")
    parser.add_argument("--config", type=Path, default=Path("config.json"), help="Path to config.json.")
    parser.add_argument("--queue", type=Path, help="Path to queue.json (default: next to config).")
    parser.add_argument("--stage", choices=STAGES, help="Run only one workflow stage.")
    parser.add_argument("--discover-only", action="store_true", help="Discover episodes without downloading them.")
    parser.add_argument("--add-episode", help="Queue one YouTube episode without running workflow stages.")
    parser.add_argument("--force", action="store_true", help="Recreate successful scrub or summary artifacts in the selected stage.")
    parser.add_argument("--verbose", action="store_true", help="Show component start, outcome, and concise diagnostics.")
    parser.add_argument("--no-live", action="store_true", help="Use append-only progress output instead of terminal redraws.")
    args = parser.parse_args(argv)
    if args.discover_only and (args.stage or args.force or args.add_episode):
        parser.error("--discover-only cannot be combined with --stage, --force, or --add-episode.")
    if args.add_episode and (args.stage or args.force):
        parser.error("--add-episode cannot be combined with --stage or --force.")
    if args.force and args.stage not in {"scrub", "summarize"}:
        parser.error("--force requires --stage scrub or --stage summarize.")
    return args


def selected_stages(args: argparse.Namespace) -> tuple[str, ...]:
    if args.discover_only:
        return ("download",)
    if args.stage is not None:
        return (args.stage,)
    return STAGES


def stage_status(stage: str, report: Any) -> tuple[str, bool, bool, list[str]]:
    """Return user status, failure flag, warning flag, and concise diagnostics."""
    if stage == "download":
        failed = report.failed_downloads + len(report.discovery_failures)
        return (
            f"{report.discovered} discovered, {report.downloaded} downloaded, {failed} failed",
            failed > 0,
            False,
            report.discovery_failures,
        )
    if stage == "scrub":
        if report.scrubbed == 0 and report.failed_scrubs == 0:
            return "no eligible episodes", False, False, []
        return f"{report.scrubbed} scrubbed, {report.failed_scrubs} failed", report.failed_scrubs > 0, False, report.failures
    if stage == "summarize":
        if report.summarized == 0 and report.failed_summaries == 0:
            return "no eligible episodes", False, False, []
        return (
            f"{report.summarized} summarized, {report.failed_summaries} failed",
            report.failed_summaries > 0,
            False,
            report.failures,
        )
    if report.delivered == 0 and report.failed_deliveries == 0:
        status = "no eligible summaries; no batch created"
    else:
        status = f"{report.delivered} delivered to Obsidian, {report.failed_deliveries} failed"
    if report.instapaper_warning is not None:
        status += "; Instapaper warning"
    diagnostics = list(report.failures)
    if report.instapaper_warning is not None:
        diagnostics.append(f"Instapaper warning: {report.instapaper_warning}")
    return status, report.failed_deliveries > 0, report.instapaper_warning is not None, diagnostics


def completion_log(stage: str, report: Any) -> str:
    if stage == "download":
        failed = report.failed_downloads + len(report.discovery_failures)
        return f"download completed: discovered={report.discovered} downloaded={report.downloaded} failed={failed}"
    if stage == "scrub":
        return f"scrub completed: scrubbed={report.scrubbed} failed={report.failed_scrubs}"
    if stage == "summarize":
        return f"summarize completed: summarized={report.summarized} failed={report.failed_summaries}"
    warning = "true" if report.instapaper_warning is not None else "false"
    return f"delivery completed: delivered={report.delivered} failed={report.failed_deliveries} instapaper_warning={warning}"


def run_workflow(
    args: argparse.Namespace,
    *,
    output: TextIO | None = None,
    diagnostics: TextIO | None = None,
) -> int:
    """Run selected stages while holding the queue's single-writer lock."""
    output = output or sys.stdout
    diagnostics = diagnostics or sys.stderr
    config_path = args.config.resolve()
    queue_path = (args.queue or config_path.with_name("queue.json")).resolve()
    try:
        with QueueLock(queue_path):
            return _run_workflow_locked(args, output=output, diagnostics=diagnostics)
    except QueueLockUnavailable as error:
        diagnostics.write(f"ERROR: {safe_message(error)}\n")
        diagnostics.flush()
        return 2


def _run_workflow_locked(
    args: argparse.Namespace,
    *,
    output: TextIO,
    diagnostics: TextIO,
) -> int:
    """Run selected stages and return the documented process exit code."""
    config_path = args.config.resolve()
    queue_path = (args.queue or config_path.with_name("queue.json")).resolve()
    logger = RunLogger(config_path.parent / "logs" / "runs")
    reporter = ProgressReporter(output, live=not args.no_live and output.isatty())
    logger.write("run started")

    try:
        config = downloader.load_config(config_path)
        queue = downloader.load_queue(queue_path, config["shows"])
    except (downloader.ConfigurationError, OSError) as error:
        message = safe_message(error)
        logger.write(f"run could not complete: exit_code=2 error={message}")
        if args.verbose:
            diagnostics.write(f"Configuration error: {message}\n")
        reporter.finish("could not complete", logger.path)
        return 2

    if args.add_episode:
        logger.write("queue add started")
        try:
            episode, added = downloader.queue_user_injected_episode(queue, args.add_episode)
        except ValueError as error:
            message = safe_message(error)
            logger.write(f"queue add errored: {message}")
            logger.write("run could not complete: exit_code=2")
            if args.verbose:
                diagnostics.write(f"Queue add error: {message}\n")
            reporter.finish("could not complete", logger.path)
            return 2

        if added:
            downloader.write_json_atomically(queue_path, queue)
            message = f"Queued: {episode['source_url']}"
            key = downloader.episode_key(episode["show_id"], episode["source_episode_id"])
            logger.write(f"queue add completed: status=queued episode_key={key}")
        else:
            message = f"Already queued: {episode['source_url']}"
            key = downloader.episode_key(episode["show_id"], episode["source_episode_id"])
            logger.write(f"queue add completed: status=already-queued episode_key={key}")
        output.write(f"{message}\n")
        output.flush()
        logger.write("run completed: exit_code=0")
        reporter.finish("completed", logger.path)
        return 0

    state = RunState()
    stage_functions = {
        "download": lambda: downloader.run_downloader(
            config, queue, queue_path, config_path.parent, discover_only=args.discover_only
        ),
        "scrub": lambda: transcript_scrubber.run_scrubber(
            config, queue, queue_path, config_path.parent, force=args.force
        ),
        "summarize": lambda: summarizer.run_summarizer(
            config, queue, queue_path, config_path.parent, force=args.force
        ),
        "delivery": lambda: delivery.run_delivery(config, queue, queue_path, config_path.parent),
    }

    for stage in selected_stages(args):
        reporter.update(stage, "attempting...")
        logger.write(f"{stage} started")
        if args.verbose:
            diagnostics.write(f"{STAGE_LABELS[stage]} started.\n")
        try:
            report = stage_functions[stage]()
        except Exception as error:
            message = safe_message(error)
            reporter.update(stage, "errored; see log")
            logger.write(f"{stage} errored: {message}")
            if args.verbose:
                diagnostics.write(f"{STAGE_LABELS[stage]} errored: {message}\n")
            logger.write("run could not complete: exit_code=2")
            reporter.finish("could not complete", logger.path)
            return 2

        status, failed, warned, stage_diagnostics = stage_status(stage, report)
        reporter.update(stage, status)
        logger.write(completion_log(stage, report))
        state.failed = state.failed or failed
        state.warned = state.warned or warned
        if args.verbose:
            diagnostics.write(f"{STAGE_LABELS[stage]} completed: {status}.\n")
            for message in stage_diagnostics:
                diagnostics.write(f"{STAGE_LABELS[stage]}: {safe_message(message)}\n")

    if state.failed:
        outcome, exit_code = "completed with failures", 1
    elif state.warned:
        outcome, exit_code = "completed with warnings", 0
    else:
        outcome, exit_code = "completed", 0
    logger.write(f"run {outcome}: exit_code={exit_code}")
    reporter.finish(outcome, logger.path)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    return run_workflow(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
