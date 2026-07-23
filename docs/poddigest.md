# Poddigest — Main Orchestrator Design Notes

`poddigest.py` is the primary user-facing command for the podcast workflow. It
coordinates the existing components without absorbing their source, transcript,
prompt, or delivery logic.

## Purpose and boundary

`poddigest.py` runs the workflow in this order:

1. `downloader.py`: discover and download transcripts.
2. `transcript_scrubber.py`: create scrubbed transcripts.
3. `summarizer.py`: create per-episode summaries.
4. `delivery.py`: create and deliver one collated reading file.

The orchestrator owns stage selection, the progress display, the final run
report, a minimal run log, and the process exit status. Individual component
commands remain supported for diagnosis and targeted rework.

It calls public `run_*` functions in-process. It does not shell out to the
component CLIs or parse their printed output. That keeps the user-facing report
consistent and prevents verbose mode from duplicating component output.

`queue.json` remains the durable source of truth for episode lifecycle and
retry state. The run log is a compact troubleshooting record, not a second
queue or a run-history product.

## CLI contract

The ordinary command is:

```text
uv run python poddigest.py
```

It defaults to `config.json` and its sibling `queue.json`.

```text
uv run python poddigest.py [--config PATH] [--queue PATH]
                            [--stage download|scrub|summarize|delivery]
                            [--discover-only | --add-episode YOUTUBE_URL]
                            [--force] [--verbose] [--no-live]
```

- Without `--stage`, run all four stages in order.
- With `--stage`, run only that stage; prerequisites are not run implicitly.
- `--discover-only` discovers episodes and exits. It is mutually exclusive
  with `--stage` and `--force`.
- `--add-episode YOUTUBE_URL` validates and queues one manually chosen YouTube
  episode, then exits without discovery or processing. It is mutually
  exclusive with `--discover-only`, `--stage`, and `--force`. It is intended
  for one-off episodes outside the monitored shows.
- `--force` requires `--stage scrub` or `--stage summarize`. It recreates
  successful artifacts only for the explicitly selected stage. It is invalid
  without a stage and never redelivers a completed batch.
- `--verbose` adds component start/finish lines, component counts, and concise
  errors to the terminal. It does not replay or expose all component CLI
  output, transcripts, prompts, credentials, or browser details.
- `--no-live` uses append-only terminal output. It is useful for redirected
  output or if terminal line replacement is unwelcome.

The orchestrator deliberately does not mirror every component option. Options
that change internals (browser headlessness, timeout, prompt, destination) stay
in `config.json`; duplicating them on the orchestrator would create conflicting
precedence rules.

### One-off episode injection

For an episode from a show that is not monitored, use:

```text
uv run python poddigest.py --add-episode "https://youtu.be/<video-id>"
```

The command accepts standard YouTube watch, short-link, Shorts, Live, and
embed URLs; normalizes the source URL; and atomically creates a pending queue
record. A repeated video ID reports `Already queued` and preserves its existing
lifecycle state. It does not create a configured show or alter monitored-show
discovery state. Run ordinary `poddigest.py` later to process the queued item.

## Progress display and final report

The standard terminal view is a compact four-row status board. Counts always
mean *this run*, not lifetime totals. A stage with no eligible work says `no
eligible episodes`; it is not reported as `0 success`.

While a stage is running on an interactive terminal:

```text
Download: 3 discovered, 3 downloaded, 0 failed
Scrub: 3 scrubbed, 0 failed
Summarize: attempting...
Delivery: pending
```

When it finishes, that row is replaced in place:

```text
Download: 3 discovered, 3 downloaded, 0 failed
Scrub: 3 scrubbed, 0 failed
Summarize: 5 summarized, 0 failed
Delivery: 5 delivered to Obsidian; Instapaper warning

Run completed with warnings. Log: logs/runs/20260723T180102Z.log
```

`Delivery` reports the durable Obsidian result separately from the non-blocking
Instapaper result. If no summaries are eligible, it says `no eligible
summaries; no batch created`.

For redirected output, CI, or `--no-live`, use append-only lines rather than
terminal-control characters. The final report is always printed once and must
be readable on its own.

## Minimal logging design

The log needs to answer one question reliably: *was each component invoked,
and did it complete or error?* Detailed episode state and retry diagnostics
remain in `queue.json`.

- Write one plain-text UTF-8 log per invocation under `logs/runs/`, named
  `<started-at>.log`, for example `20260723T180102Z.log`.
- Treat `logs/` as generated local operational data and ignore it in Git.
- Record a start line, a start and completion line for each invoked component,
  and one final outcome line. Completion lines include the component's
  aggregate counts. Error lines include a concise, safe error message.
- Never write credentials, `.env` values, transcript text, prompt text,
  browser cookies, or full command input to the log. Do not add debug
  tracebacks in this increment.
- Print the path in the final report. There is no `history` command and no
  automatic retention or pruning policy in this manual v1.

Example:

```text
2026-07-23T18:01:02Z run started
2026-07-23T18:01:02Z download started
2026-07-23T18:02:14Z download completed: discovered=3 downloaded=3 failed=0
2026-07-23T18:02:14Z scrub started
2026-07-23T18:02:15Z scrub completed: scrubbed=3 failed=0
2026-07-23T18:02:15Z summarize started
2026-07-23T18:12:03Z summarize completed: summarized=3 failed=0
2026-07-23T18:12:03Z delivery started
2026-07-23T18:12:04Z delivery completed: delivered=3 failed=0 instapaper_warning=true
2026-07-23T18:12:04Z run completed with warnings: exit_code=0
```

## Execution and recovery rules

1. Load and validate config and queue once. Resolve all paths once and pass
   the same in-memory queue to component functions.
2. Queue state after each component/episode remains the recovery mechanism if
   the orchestrator is interrupted.
3. One failed episode does not stop later eligible work. The orchestrator
   continues to delivery even when another episode failed earlier in the run:
   delivery batches the summaries that actually succeeded. The final report
   still makes the missing/failed work visible and returns exit code 1.
4. A non-blocking Instapaper warning does not undo a successful delivery or
   cause redelivery on the next run.
5. The orchestrator owns exit semantics. Component `main()` functions retain
   their standalone behavior.

| Code | Outcome | Example |
|---|---|---|
| 0 | completed, including warnings | Obsidian delivered; Instapaper failed. |
| 1 | completed with retryable processing failure | one transcript or summary failed. |
| 2 | could not safely start or continue | invalid config, unreadable queue, unexpected orchestration failure. |

The final line says `completed`, `completed with warnings`, `completed with
failures`, or `could not complete`; it never forces the user to infer the
outcome from the exit code alone.

## Scheduling and concurrency

Every executable command takes a non-blocking single-writer lock around its
queue lifecycle. The stable lock is the ignored sibling `.queue.json.lock`,
because `queue.json` itself is atomically replaced during state updates. A
busy queue exits with code 2 before a component or run log is started.

The macOS LaunchAgent setup lives in [scheduling.md](scheduling.md). It runs
the ordinary orchestrator every Friday at 3:00 p.m. local time and safely
skips rather than overlaps an active manual invocation.

## Deferred decisions

- Per-episode live progress is out of scope. It would require callbacks from
  component loops so the orchestrator could show granular progress such as
  `2 of 5`; the current contract only needs a truthful stage-level
  `attempting...` state.
- Redelivery of an already delivered batch is not part of this command yet.
  The standalone `delivery.py` remains the targeted recovery path.

## Acceptance criteria for the first implementation

- One ordinary invocation executes the full workflow through the public
  component functions, creates one minimal run log, and emits exactly one final
  report.
- The log states the start and outcome for every invoked component and contains
  no sensitive content.
- An interactive terminal shows a bounded four-row live board; redirected
  output contains no control characters and remains readable.
- Queue updates, artifact locations, and component-level retry behavior are
  unchanged from the current tested contracts.
- A failed episode produces a queue error and a failed component result in the
  run log; later eligible episodes are still processed and delivered.
- An Instapaper failure is visible as a warning, retains successful Obsidian
  delivery, and exits 0 when no other failure occurs.
