# Podcast Summarizer — Plan and Design Notes

This is a living record of decisions and system-design thinking. It is intentionally lightweight: it should guide the next increment without pretending that every future choice has been made.

## Product boundary for v1

A manual Python command-line tool checks shows for episodes published since its last successful source check, queues newly discovered episodes, downloads and normalizes transcripts from one selected transcript source, produces an editable-template Markdown summary for each normalized episode, creates a collated Markdown reading file, copies that file to a configured Obsidian-vault folder, optionally creates an Instapaper reminder, and reports successes and failures. Raw, scrubbed, and per-episode summary artifacts remain in their current folders.

Automatic scheduling uses a per-user macOS LaunchAgent; see [scheduling.md](scheduling.md). Instapaper is a best-effort, non-blocking reminder: its failure is reported but does not undo a successful Obsidian delivery.

## Working architecture hypothesis

The source adapter, transcript scrubber, summarizer, storage/state, and delivery steps should be separate modules.

The CLI orchestrates them; it should not contain source-specific scraping or prompt logic.

```
User or scheduler
    |
    | config.json: shows and provider URLs
    | queue.json: last successful check and episode queue, keyed by source ID/URL
    V
Downloader.py  discover episodes since the last successful check,
    |            add new source IDs/URLs to the queue, and retrieve transcripts
    |
    | raw transcripts for each episode
    V 
TranscriptScrubber.py  remove source formatting without changing discussion content
    |
    | normalized transcripts for each episode
    V
Summarizer.py  take queued normalized transcripts and invoke the selected local AI CLI,
    |            using `summary_prompt.md`
    |
    |  summary.md for each episode 
    V
delivery.py         create one collated Markdown reading file
    |                move collated Markdown reading file to Obsidian
    |                create a non-blocking Instapaper reminder
    |
    V
  (end)
```

## Decisions made

| Area | Decision | Rationale |
|---|---|---|
| Initial shows | Configurable | Real, representative sources for the first build. |
| Invocation | Manual CLI | Gives observable, debuggable runs before adding a scheduler. |
| Transcript source | One source, selected by a spike | Keeps v1 reliable and simple; no fallback logic initially. |
| AI integration | Locally installed Codex CLI | Avoids API integration in the first build and keeps prompts user-editable. |
| State | Simple JSON tracker | Appropriate for a personal, file-first tool. |
| Discovery and processing state | Track each source's last successful check plus an episode queue keyed by source ID/URL and lifecycle status | Separates discovery from summarization, supports retries, and prevents duplicate work. This is a standing v1 hypothesis to validate. |
| Transcript normalization | Keep the downloaded raw transcript and create a separate scrubbed transcript before summarization | Source formatting should not leak into the prompt, while retaining the original makes scraper changes inspectable and re-runnable. |
| Failure handling | Continue processing other episodes; save/report failures for a later retry | A single bad page must not hide successful work. |
| Delivery | Copy collated Markdown to a configured Obsidian-vault folder and make a best-effort Instapaper reminder | Obsidian is the durable, inspectable reading artifact; Instapaper is a convenient alert and never blocks delivery. |
| Local artifacts | Keep raw, scrubbed, and per-episode summary files in their existing folders | Preserves provenance and makes every earlier stage inspectable and re-runnable. |
