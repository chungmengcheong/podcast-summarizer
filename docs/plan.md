# Podcast Summarizer — Plan and Design Notes

This is a living record of decisions and system-design thinking. It is intentionally lightweight: it should guide the next increment without pretending that every future choice has been made.

## Product boundary for v1

A manual Python command-line tool checks All-In and 20VC for episodes published since its last successful source check, queues newly discovered episodes, downloads and normalizes transcripts from one selected transcript source, produces an editable-template Markdown summary for each normalized episode, creates a collated Markdown reading file, copies that file to a configured Obsidian-vault folder, organizes local artifacts by show, and reports successes and failures.

Automatic scheduling and Instapaper delivery are deferred increments.

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
post_processor.py   create one collated Markdown reading file
    |                archive transcripts and summaries for each episode
    |                move collated Markdown reading file to Obsidian
    |
    V
  (end)
```

## Decisions made

| Area | Decision | Rationale |
|---|---|---|
| Initial shows | All-In and 20VC | Real, representative sources for the first build. |
| Invocation | Manual CLI | Gives observable, debuggable runs before adding a scheduler. |
| Transcript source | One source, selected by a spike | Keeps v1 reliable and simple; no fallback logic initially. |
| AI integration | Locally installed Codex or Claude CLI | Avoids API integration in the first build and keeps prompts user-editable. |
| State | Simple JSON tracker | Appropriate for a personal, file-first tool. |
| Discovery and processing state | Track each source's last successful check plus an episode queue keyed by source ID/URL and lifecycle status | Separates discovery from summarization, supports retries, and prevents duplicate work. This is a standing v1 hypothesis to validate. |
| Transcript normalization | Keep the downloaded raw transcript and create a separate scrubbed transcript before summarization | Source formatting should not leak into the prompt, while retaining the original makes scraper changes inspectable and re-runnable. |
| Failure handling | Continue processing other episodes; save/report failures for a later retry | A single bad page must not hide successful work. |
| Delivery | Copy collated Markdown to a configured Obsidian-vault folder | Produces a durable, inspectable reading artifact without external-account integration. |

## Proposed implementation increments

1. **Single-source ingestion:** Discover and download one new transcript per supported show; persist normalized metadata and local files. Completed for All-In and 20VC.
2. **Transcript normalization:** Given a downloaded raw transcript, create and persist a scrubbed transcript using deterministic, source-aware formatting rules. Add unit tests for every observed source format.
3. **Local summarization:** Given a scrubbed transcript file, generate a summary file using the editable prompt/template. No additional scraping or Obsidian copy.
4. **End-to-end run:** Add full multi-episode lifecycle handling, artifact organization, a collated reading file, Obsidian copy, and a clear run report.
5. **Reliability pass:** Add per-episode error records, safe retries on later runs, and tests around state and file organization.
6. **Scheduling:** Add a Mac-appropriate scheduled invocation once the manual command is trusted.
7. **Optional Instapaper delivery:** Investigate and add it as a separate integration, if still useful after the Obsidian workflow is established.


