#!/usr/bin/env python3
"""Deliver batches of generated podcast summaries to Obsidian and Instapaper."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from downloader import ConfigurationError, load_config, load_queue, stage_state, utc_now, write_json_atomically
from transcript_scrubber import write_text_atomically


InstapaperSender = Callable[[str, str, str, str, str], None]


class DeliveryError(RuntimeError):
    """Raised when a delivery batch cannot be created safely."""


@dataclass
class DeliveryReport:
    delivered: int = 0
    failed_deliveries: int = 0
    failures: list[str] = field(default_factory=list)
    local_batch_path: Path | None = None
    obsidian_path: Path | None = None
    instapaper_warning: str | None = None


def load_delivery_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate delivery-specific configuration."""
    paths = config.get("paths")
    if not isinstance(paths, dict) or not isinstance(paths.get("transcripts_dir"), str):
        raise ConfigurationError("config.paths must define transcripts_dir.")

    delivery = config.get("delivery")
    if not isinstance(delivery, dict):
        raise ConfigurationError("config.delivery must be an object.")
    target_dir = delivery.get("obsidian_target_dir")
    if not isinstance(target_dir, str) or not target_dir.strip():
        raise ConfigurationError("config.delivery.obsidian_target_dir must be a non-empty path.")
    if not Path(target_dir).expanduser().is_absolute():
        raise ConfigurationError("config.delivery.obsidian_target_dir must be an absolute path.")
    instapaper_url = delivery.get("instapaper_url")
    if not isinstance(instapaper_url, str) or not instapaper_url.startswith(("http://", "https://")):
        raise ConfigurationError("config.delivery.instapaper_url must be an HTTP(S) URL.")
    return delivery


def delivery_state(episode: dict[str, Any]) -> dict[str, Any]:
    """Get or create the lifecycle state for an episode's durable delivery."""
    state = episode.setdefault("delivery", stage_state("pending"))
    if not isinstance(state, dict):
        raise DeliveryError("Episode delivery state must be an object.")
    return state


def pending_deliveries(queue: dict[str, Any]) -> list[dict[str, Any]]:
    """Return summaries not yet durably copied to Obsidian, newest first."""
    eligible: list[dict[str, Any]] = []
    for episode in queue["episodes"].values():
        if episode.get("summary", {}).get("status") != "succeeded":
            continue
        state = episode.get("delivery")
        if state is None or (isinstance(state, dict) and state.get("status") in {"pending", "failed"}):
            eligible.append(episode)
    return sorted(eligible, key=lambda episode: episode.get("published_at", ""), reverse=True)


def validated_summary_path(config_root: Path, episode: dict[str, Any]) -> Path:
    raw_path = episode.get("summary_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise DeliveryError("Episode has no summary_path.")
    candidate = (config_root / raw_path).resolve()
    try:
        candidate.relative_to(config_root.resolve())
    except ValueError as error:
        raise DeliveryError("Episode summary_path must stay inside the repository.") from error
    if not candidate.is_file():
        raise DeliveryError(f"Summary file does not exist: {raw_path}")
    return candidate


def parse_published_date(episode: dict[str, Any]) -> date:
    value = episode.get("published_at")
    if not isinstance(value, str):
        raise DeliveryError("Episode has no published_at date.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise DeliveryError("Episode published_at must be an ISO date.") from error


def batch_title(episodes: list[dict[str, Any]]) -> str:
    if not episodes:
        raise DeliveryError("Cannot create a batch without summaries.")
    dates = [parse_published_date(episode) for episode in episodes]
    return f"{min(dates):%Y%m%d}-{max(dates):%Y%m%d}-{len(episodes)} summaries"


def build_collated_markdown(title: str, summary_texts: list[str]) -> str:
    if not summary_texts:
        raise DeliveryError("Cannot create a collated file without summary content.")
    normalized = [text.strip() for text in summary_texts]
    if any(not text for text in normalized):
        raise DeliveryError("Summary file was empty.")
    return f"# {title}\n\n" + "\n\n---\n\n".join(normalized) + "\n"


def write_obsidian_copy(destination: Path, content: str) -> None:
    """Write once, or accept an identical file left by an interrupted earlier run."""
    if destination.exists():
        try:
            existing = destination.read_text(encoding="utf-8")
        except OSError as error:
            raise DeliveryError(f"Could not read existing Obsidian file: {error}") from error
        if existing != content:
            raise DeliveryError(f"Obsidian destination already exists with different content: {destination}")
        return
    write_text_atomically(destination, content)


def load_dotenv_values(path: Path) -> dict[str, str]:
    """Read the small KEY=VALUE subset needed for local, ignored credentials."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped.removeprefix("export ").lstrip()
        key, separator, value = stripped.partition("=")
        if not separator or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def instapaper_credentials(config_root: Path) -> tuple[str, str]:
    values = load_dotenv_values(config_root / ".env")
    # A repository-local .env is the normal user-owned configuration. The
    # environment remains a useful fallback for scheduled invocations, but
    # must not silently replace a deliberate project-specific setting.
    username = values.get("INSTAPAPER_USERNAME") or os.environ.get("INSTAPAPER_USERNAME")
    password = values.get("INSTAPAPER_PASSWORD") or os.environ.get("INSTAPAPER_PASSWORD")
    if not username or not password:
        raise DeliveryError("Instapaper credentials are missing from .env or the environment.")
    return username, password


def send_instapaper_reminder(username: str, password: str, url: str, title: str, api_url: str) -> None:
    """Create one Instapaper stub reminder using the documented simple API."""
    data = urlencode({"username": username, "password": password, "url": url, "title": title}).encode()
    request = Request(api_url, data=data, method="POST")
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - API URL is local configuration.
            if not 200 <= response.status < 300:
                raise DeliveryError(f"Instapaper returned HTTP {response.status}.")
    except HTTPError as error:
        raise DeliveryError(f"Instapaper returned HTTP {error.code}.") from error
    except URLError as error:
        raise DeliveryError(f"Instapaper request failed: {error.reason}.") from error


def fail_deliveries(episodes: list[dict[str, Any]], error: Exception, report: DeliveryReport) -> None:
    message = str(error)
    for episode in episodes:
        state = delivery_state(episode)
        state.update({"status": "failed", "completed_at": None, "last_error": message})
        report.failed_deliveries += 1
        report.failures.append(f"{episode.get('source_episode_id', 'unknown')}: {message}")


def run_delivery(
    config: dict[str, Any],
    queue: dict[str, Any],
    queue_path: Path,
    config_root: Path,
    instapaper_sender: InstapaperSender = send_instapaper_reminder,
) -> DeliveryReport:
    """Create and deliver one batch from all successful, undelivered summaries."""
    delivery_config = load_delivery_config(config)
    report = DeliveryReport()
    candidates = pending_deliveries(queue)
    eligible: list[dict[str, Any]] = []
    summary_texts: list[str] = []

    for episode in candidates:
        try:
            summary_path = validated_summary_path(config_root, episode)
            parse_published_date(episode)
            text = summary_path.read_text(encoding="utf-8")
            if not text.strip():
                raise DeliveryError("Summary file was empty.")
            eligible.append(episode)
            summary_texts.append(text)
        except Exception as error:
            state = delivery_state(episode)
            state["attempt_count"] = state.get("attempt_count", 0) + 1
            state["last_attempt_at"] = utc_now()
            state.update({"status": "failed", "completed_at": None, "last_error": str(error)})
            report.failed_deliveries += 1
            report.failures.append(f"{episode.get('source_episode_id', 'unknown')}: {error}")
            write_json_atomically(queue_path, queue)

    if not eligible:
        return report

    for episode in eligible:
        state = delivery_state(episode)
        state["attempt_count"] = state.get("attempt_count", 0) + 1
        state["last_attempt_at"] = utc_now()
        state["last_error"] = None
    write_json_atomically(queue_path, queue)

    try:
        title = batch_title(eligible)
        content = build_collated_markdown(title, summary_texts)
        local_path = config_root / config["paths"]["transcripts_dir"] / "collated" / f"{title}.md"
        write_text_atomically(local_path, content)
        obsidian_path = Path(delivery_config["obsidian_target_dir"]).expanduser() / local_path.name
        write_obsidian_copy(obsidian_path, content)
    except Exception as error:
        fail_deliveries(eligible, error, report)
        write_json_atomically(queue_path, queue)
        return report

    completed_at = utc_now()
    for episode in eligible:
        state = delivery_state(episode)
        episode["delivery"] = stage_state("succeeded") | {
            "attempt_count": state["attempt_count"],
            "last_attempt_at": state["last_attempt_at"],
            "completed_at": completed_at,
            "batch_path": str(local_path.relative_to(config_root)),
            "obsidian_path": str(obsidian_path),
        }
    write_json_atomically(queue_path, queue)
    report.delivered = len(eligible)
    report.local_batch_path = local_path
    report.obsidian_path = obsidian_path

    try:
        username, password = instapaper_credentials(config_root)
        instapaper_sender(
            username,
            password,
            delivery_config["instapaper_url"],
            title,
            "https://www.instapaper.com/api/add",
        )
    except Exception as error:
        report.instapaper_warning = str(error)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collate and deliver generated podcast summaries.")
    parser.add_argument("--config", type=Path, required=True, help="Path to config.json.")
    parser.add_argument("--queue", type=Path, help="Path to queue.json (default: next to config).")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    try:
        config = load_config(config_path)
        queue_path = (args.queue or config_path.with_name("queue.json")).resolve()
        queue = load_queue(queue_path, config["shows"])
        report = run_delivery(config, queue, queue_path, config_path.parent)
    except (ConfigurationError, OSError, DeliveryError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    print(f"Delivered summaries: {report.delivered}")
    if report.local_batch_path is not None:
        print(f"Collated file: {report.local_batch_path}")
    if report.obsidian_path is not None:
        print(f"Obsidian file: {report.obsidian_path}")
    print(f"Failed deliveries: {report.failed_deliveries}")
    for failure in report.failures:
        print(f"Delivery failed: {failure}", file=sys.stderr)
    if report.instapaper_warning is not None:
        print(f"Instapaper warning: {report.instapaper_warning}", file=sys.stderr)
    return 1 if report.failed_deliveries else 0


if __name__ == "__main__":
    sys.exit(main())
