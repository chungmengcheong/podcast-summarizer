# Podcast Summarizer

A personal, file-first macOS CLI that turns new podcast episodes into an
editable reading queue. It watches configured YouTube channels, retrieves
their transcripts, produces evidence-aware Markdown summaries with a local
Codex CLI, and delivers one collated note to an Obsidian folder.

This is an MVP for an individual workflow, not a hosted service. It is built
to be inspectable and recoverable: the original transcript, scrubbed
transcript, per-episode summary, queue state, and run log are all local files.

## What it does

1. Discovers new videos from configured YouTube channel pages.
2. Downloads transcripts through the YouTube Transcript browser UI.
3. Keeps the raw source text and writes a deterministic scrubbed copy.
4. Sends the scrubbed transcript to the locally installed Codex CLI using an
   editable prompt.
5. Collates newly completed summaries into one Markdown file, copies it to
   Obsidian, and optionally creates an Instapaper reminder.
6. Can run manually or through a macOS LaunchAgent every Friday at 3:00 p.m.
   local time.

```text
YouTube channels -> raw transcript -> scrubbed transcript -> Codex summary
                                                        -> Obsidian reading note
```

## Requirements

- macOS with a signed-in desktop session. The transcript provider may require
  a visible Chromium window on an initial or expired session.
- [uv](https://docs.astral.sh/uv/) for Python environment and dependency
  management.
- The [Codex CLI](https://openai.com/index/codex-now-generally-available/)
  installed, authenticated, and available as `codex` on your `PATH`.
- Access to an Obsidian vault folder if you want durable delivery. Instapaper
  is optional.

## Install on a Mac

```bash
git clone <your-fork-or-repository-url>
cd podcast-summarizer

# Creates the locked Python environment (Python 3.12+ is required).
uv sync --locked

# Installs the Chromium binary used by Playwright.
uv run playwright install chromium
```

Install and sign in to the Codex CLI before the first full run. One supported
installation route is:

```bash
npm install -g @openai/codex
codex login
```

Confirm that `codex` works from the same Terminal in which you will run the
project.

## Configure your local workflow

Edit [config.json](config.json) before running:

- `shows`: the YouTube channel `/videos` pages to monitor. Set `enabled` to
  `false` for any show you do not want.
- `delivery.obsidian_target_dir`: an absolute path to a folder in your Obsidian
  vault. This is where collated reading notes are copied.
- `summarizer.executable`: use `codex` when the Codex CLI is on your `PATH`, or
  set an absolute executable path.
- `downloader.headless`: leave this as `false` for the first successful run so
  you can complete any transcript-provider prompt. Consider `true` only after
  the browser session has proved stable.

To enable the optional Instapaper reminder, create a repository-local `.env`
file (it is ignored by Git):

```text
INSTAPAPER_USERNAME=your-username
INSTAPAPER_PASSWORD=your-password
```

Instapaper is deliberately non-blocking: a missing credential or failed
request produces a warning after successful Obsidian delivery, not a failed
batch.

> **Before publishing a fork:** `config.json` is user-owned and currently
> tracked. Review or replace its vault path and show list before committing
> your own configuration. Never commit `.env`, transcripts, `queue.json`, or
> run logs.

## Run it

Start with discovery only. It updates `queue.json` but does not open the
transcript browser:

```bash
uv run python poddigest.py --discover-only
```

Then run the complete workflow:

```bash
uv run python poddigest.py
```

The first full run may open Chromium. If the transcript provider asks for an
anonymous login or other interaction, complete it in that window. Progress and
the final outcome print to the terminal; a compact operational log is written
under `logs/runs/`.

Useful targeted commands:

```bash
# Queue one YouTube episode without adding its channel to the monitored list.
uv run python poddigest.py --add-episode "https://youtu.be/<video-id>"

# Retry only one stage. Prerequisites are not run implicitly.
uv run python poddigest.py --stage download
uv run python poddigest.py --stage scrub
uv run python poddigest.py --stage summarize
uv run python poddigest.py --stage delivery

# Regenerate successful scrubbed transcripts or summaries after changing their inputs.
uv run python poddigest.py --stage scrub --force
uv run python poddigest.py --stage summarize --force
```

The queue retains per-stage success, failure, and retry information. An
ordinary later run retries only pending or failed work; it does not download or
summarize completed episodes again.

## Schedule it with launchd

After at least one manual run succeeds, install the per-user LaunchAgent:

```bash
./scripts/install-launchd.sh
```

The installer generates a local plist using the path of your clone, registers
it for the current macOS user, and schedules the normal workflow every Friday
at 3:00 p.m. in that Mac's local time zone. It does not trigger an immediate
run. The job writes redirected output to `logs/launchd/`.

Confirm the schedule:

```bash
launchctl print gui/$(id -u)/com.ccm.podcast-summarizer
```

The workflow uses a non-blocking queue lock. If a manual run is active when
the scheduled job starts, the scheduled run exits safely instead of competing
for `queue.json`.

For troubleshooting, changing the schedule, or removing the job, see
[docs/scheduling.md](docs/scheduling.md).

## Project map

| Path | Role |
|---|---|
| `poddigest.py` | Main command that coordinates the full workflow. |
| `downloader.py` | Channel discovery and transcript download. |
| `transcript_scrubber.py` | Deterministic transcript normalization. |
| `summarizer.py` | Codex CLI invocation and output validation. |
| `delivery.py` | Collation, Obsidian delivery, and optional Instapaper reminder. |
| `prompts/summary_prompt.md` | Editable summary instructions. |
| `queue.json` | Ignored runtime state and retry record. |
| `docs/` | Component contracts and operating notes. |

## Validate changes

```bash
uv run pytest -q
```

The test suite uses local fixtures and mocks; it does not call YouTube, Codex,
Obsidian, or Instapaper.
