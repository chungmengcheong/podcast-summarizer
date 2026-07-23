# Podcast Summarizer — Scheduled Operation

The scheduler uses a per-user macOS `LaunchAgent`, not cron. It runs the same
ordinary `poddigest.py` workflow at 15:00 local time every Friday, writing its
standard output and errors to `logs/launchd/`. The normal per-run operational
log remains under `logs/runs/`.

## Single-writer protection

Every shipped command that reads or changes `queue.json` takes a non-blocking
exclusive lock first. The lock is the ignored sibling `.queue.json.lock`, not
the JSON file itself: queue updates atomically replace `queue.json`, which
would make a lock on the JSON file ineffective after its first update.

If a manual run is already active when launchd starts, the scheduled invocation
exits with code 2 and writes a clear `already using queue.json` message to the
launchd error log. It does not wait, interfere with the manual run, or modify
the queue. The operating system releases the advisory lock when a process
exits, including after an interruption.

## Install

From the repository root, run:

```text
./scripts/install-launchd.sh
```

The installer substitutes your local repository path into the tracked template,
then writes it to `~/Library/LaunchAgents/com.ccm.podcast-summarizer.plist`,
loads it for the current macOS login session, and enables it. No run is
triggered during installation. To verify the job is registered:

```text
launchctl print gui/$(id -u)/com.ccm.podcast-summarizer
```

To run it once on demand (useful after installing), use:

```text
launchctl kickstart -k gui/$(id -u)/com.ccm.podcast-summarizer
```

The scheduled runner uses `uv run --locked` and a known Homebrew/system PATH,
then calls `poddigest.py --no-live`. It therefore sees the project-local
`.env` for the optional Instapaper reminder, while keeping redirected output
free of terminal control sequences.

## Operational caveat

The current downloader is configured with `headless: false`. This retains its
browser session and makes occasional transcript-provider prompts visible, but
it also means a run needing human interaction cannot complete unattended. The
queue records that failure and retries the episode on a later run; the other
episodes continue. Once the provider session is stable for several scheduled
runs, changing `config.json` to `headless: true` is the next practical test.

## Change or remove

The tracked plist template is the schedule source of truth. Its `Weekday: 5`,
`Hour: 15`, and `Minute: 0` mean Friday at 3:00 p.m. in the Mac's configured
local time zone (Pacific time on this Mac). Edit those values, then rerun the
installer to reload the job. To stop and remove the installed job:

```text
launchctl bootout gui/$(id -u)/com.ccm.podcast-summarizer
rm ~/Library/LaunchAgents/com.ccm.podcast-summarizer.plist
```
