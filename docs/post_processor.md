# Podcast Summarizer — Post-processor Design Notes

## Purpose and boundary

The post-processor turns a batch of successfully generated episode summaries
into one Markdown reading file, delivers that file to the configured Obsidian
folder, and creates a best-effort Instapaper reminder. It does not generate
summaries, alter raw or scrubbed transcripts, or reorganize local artifacts.

The Obsidian copy is the durable delivery result. Instapaper is a convenience:
its failure must be visible in the run report but must not make the batch or
the included episodes eligible for redelivery.

## Configuration

`config.json` must define the following post-processor settings:

```json
"post_processor": {
  "obsidian_target_dir": "/Users/ccm/Library/Mobile Documents/iCloud~md~obsidian/Documents/Notes/z_to_read",
  "instapaper_url": "http://www.nourl.com"
}
```

The target folder is configurable and is never hard-coded in the module.
Instapaper credentials stay in the ignored `.env` file as
`INSTAPAPER_USERNAME` and `INSTAPAPER_PASSWORD`; they are never copied to the
configuration file, queue, logs, or errors.

## Input contract

An episode is eligible for a new batch when all of the following hold:

- `summary.status` is `succeeded`.
- `summary_path` identifies an existing UTF-8 Markdown summary under the
  repository.
- Its `delivery.status` is absent, `pending`, or `failed`.
- `published_at` is a valid `YYYY-MM-DD` date.

The processor loads the selected summary files from `summary_path` rather than
deriving filenames. This preserves the summarizer as the owner of the
per-episode artifact convention.

## Output contract

For each nonempty batch, the processor creates a UTF-8 Markdown file named:

```
<earliest_published_date>-<latest_published_date>-<number_of_summaries> summaries.md
```

Dates use `YYYYMMDD`; for example,
`20260710-20260717-2 summaries.md`.

The document begins with the corresponding H1 title without the `.md`
extension. It then includes the complete episode summaries in reverse
publication-date order (newest first), separated by a Markdown horizontal rule
(`---`). A summary's existing H1 heading remains unchanged.

The processor first writes the batch artifact locally under
`transcripts/collated/`, then copies it to `obsidian_target_dir`. Local raw,
scrubbed, and per-episode summary files remain in their existing folders.

## State and idempotency

Delivery is recorded per episode in `queue.json`:

```json
"delivery": {
  "status": "succeeded",
  "attempt_count": 1,
  "last_attempt_at": "2026-07-23T00:00:00Z",
  "completed_at": "2026-07-23T00:00:01Z",
  "last_error": null,
  "batch_path": "transcripts/collated/20260710-20260717-2 summaries.md",
  "obsidian_path": "/.../20260710-20260717-2 summaries.md"
}
```

Before copying, increment and persist each selected episode's
`delivery.attempt_count` and `last_attempt_at`. Mark all selected episodes as
`succeeded` only after the Obsidian file exists with the expected content, then
persist the batch paths. A failed local write or Obsidian copy leaves the
episodes retryable and records the concise error.

If a run is interrupted after the Obsidian copy but before state is persisted,
a later run may encounter the same destination filename. When its contents
match the newly generated batch, treat that as a completed copy and safely
advance state; when they differ, fail visibly rather than overwrite it.

## Workflow

1. Select eligible summaries and sort them by `published_at` descending. If
   none are eligible, report that no batch was created and make no network
   call.
2. Compute the title from the oldest and newest publication dates and the
   number of selected summaries. Build the collated document and write it
   atomically to `transcripts/collated/`.
3. Copy the collated file to the configured Obsidian folder, applying the
   idempotency check above.
4. Mark the included episodes delivered and report the local and Obsidian
   paths.
5. Make one best-effort Instapaper `POST` to
   `https://www.instapaper.com/api/add` with `username`, `password`, `url`,
   and the batch title. Read the credentials only at this step.
6. If Instapaper rejects the request, times out, or credentials are missing,
   retain the successful Obsidian delivery, emit a clear warning in the CLI
   report, and exit successfully unless another delivery operation failed.

The first implementation makes a single Instapaper attempt for each newly
delivered batch. It does not retry automatically, which avoids duplicate
reminders after an ambiguous network failure. A dedicated retry mechanism can
be added if it becomes useful.

## CLI and run report

`post_processor.py` accepts `--config` and an optional `--queue`, following the
existing downloader and summarizer convention. Its report states:

- number of summaries delivered;
- the local collated file and Obsidian destination;
- the count of retryable delivery failures; and
- an `Instapaper warning:` line when the non-blocking reminder failed.

## Validation and acceptance

Unit tests cover ordering, title construction, Markdown composition, absent or
invalid delivery inputs, queue-state transitions, atomic local output, copy
retries, and the no-overwrite-on-content-mismatch rule. Network calls are
mocked. Manual acceptance uses the existing two successful summaries, verifies
the resulting Obsidian note, and confirms that an Instapaper failure produces a
warning without making either episode pending again.
