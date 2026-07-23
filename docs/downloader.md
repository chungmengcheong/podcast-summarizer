# Podcast Summarizer — Downloader Plan and Design Notes

This is a living record of decisions and system-design thinking. 

## Purpose

The downloader component discovers episodes, records newly seen source IDs/URLs in the queue, accepts one-off YouTube episodes, and retrieves transcripts.

## Input / Output contract

### Input

#### `config.json`

`config.json` is user-owned configuration. It names the shows to monitor and the local paths the tool may use. It does not change after a normal run.

This is the complete v1 shape. Paths are relative to the repository root so the project can move as a directory. Adding a second show requires no code change.

```json
{
  "version": 1,
  "paths": {
    "transcripts_dir": "transcripts",
    "browser_profile_dir": ".playwright-profile/youtube-transcript"
  },
  "downloader": {
    "headless": false,
    "page_timeout_seconds": 120,
    "request_pause_seconds": 10,
    "max_new_episodes_per_show": 3
  },
  "shows": [
    {
      "id": "all-in",
      "display_name": "All-In",
      "youtube_videos_url": "https://www.youtube.com/@allin/videos",
      "enabled": true
    },
    {
      "id": "20vc",
      "display_name": "20VC",
      "youtube_videos_url": "https://www.youtube.com/@20VC/videos",
      "enabled": true
    }
  ]
}
```

Rules:

- `id` is a lowercase, hyphenated local identifier and must not change once episodes for the show exist in the queue.
- `youtube_videos_url` must be the channel's videos page, not an individual video or a hand-curated playlist.
- `headless` stays `false` for the initial increment so an expired anonymous session is observable and recoverable. It can be made `true` later for a scheduled run after reliable bootstrap behavior is established.
- `max_new_episodes_per_show` is a first-run/backlog guard, not a normal discovery limit. A later run should process every newly discovered ID.
- The initial default is three episodes for each newly added show.
- `request_pause_seconds` must be at least three. The downloader randomizes the pause after each attempt between three seconds and this configured upper bound.



### Output

#### Downloaded transcripts

1. A set of unmodified downloaded transcripts in `transcripts/raw/`. Each file is named `<YYYYMMDD>-<show-id>-<video-id>-<episode-slug>.txt`, for example:

```
transcripts/raw/20260717-all-in-PHL1j2ti420-more-trillion-dollar-ipos-anthropic-3t-zucks-price-war.txt
```

The source video ID makes a filename unique even if titles change or two episodes have the same title. The title slug is for human recognition only. The downloader never edits these source records; `transcript_scrubber.py` writes matching normalized files to `transcripts/scrubbed/`.

For queues created before this layout, the scrubber recognizes a missing legacy `transcripts/<filename>.txt` path and adopts the matching file in `transcripts/raw/`, updating the queue as it does so.


#### `queue.json`

`queue.json` is tool-owned operational state. It records discovered episodes, the outcome of each stage, and each show's last successful check. It stores all discovered episodes, not only the currently pending ones. This makes it both the work queue and the durable record used to avoid rediscovering old episodes.

An empty queue is valid initial state:

```json
{
  "version": 1,
  "shows": {
    "all-in": {
      "last_successful_check_at": null
    },
    "20vc": {
      "last_successful_check_at": null
    }
  },
  "episodes": {}
}
```

After a successful discovery and download, it has this shape:

```json
{
  "version": 1,
  "shows": {
    "all-in": {
      "last_successful_check_at": "2026-07-22T18:30:00Z"
    }
  },
  "episodes": {
    "youtube:all-in:PHL1j2ti420": {
      "show_id": "all-in",
      "source": "youtube-transcript-ui",
      "source_episode_id": "PHL1j2ti420",
      "source_url": "https://www.youtube.com/watch?v=PHL1j2ti420",
      "title": "More Trillion Dollar IPOs, Anthropic $3T, Zuck's Price War...",
      "published_at": "2026-07-17",
      "discovered_at": "2026-07-22T18:30:00Z",
      "raw_transcript_path": "transcripts/raw/20260717-all-in-PHL1j2ti420-more-trillion-dollar-ipos-anthropic-3t-zucks-price-war.txt",
      "scrubbed_transcript_path": null,
      "download": {
        "status": "succeeded",
        "attempt_count": 1,
        "last_attempt_at": "2026-07-22T18:31:12Z",
        "completed_at": "2026-07-22T18:31:25Z",
        "last_error": null
      },
      "scrub": {
        "status": "pending",
        "attempt_count": 0,
        "last_attempt_at": null,
        "completed_at": null,
        "last_error": null
      },
      "summary": {
        "status": "not_ready",
        "attempt_count": 0,
        "last_attempt_at": null,
        "completed_at": null,
        "last_error": null
      }
    }
  }
}
```

The key is `source:show-id:source-episode-id`; the same three values are also stored inside the record to keep the file readable and independently recoverable. `published_at` may be `null` if YouTube does not supply it during the initial discovery pass; it must be filled before the final transcript file is named.

### One-off YouTube episodes

`--add-episode <youtube-url>` is a queue-only operation for episodes from
shows that are not monitored. It accepts standard watch, short-link, Shorts,
Live, and embed video URLs, normalizes them to a canonical watch URL, and
creates this record shape:

```json
{
  "show_id": "user-injected",
  "show_display_name": "User-injected",
  "source_episode_id": "<youtube-video-id>",
  "source_url": "https://www.youtube.com/watch?v=<youtube-video-id>",
  "title": "Manually added episode",
  "published_at": null
}
```

The normal download step fills title and publication date, and uses the
YouTube channel name as `show_display_name` when it is present. The queue key
is `youtube:user-injected:<youtube-video-id>`. A video ID already present in
the queue—whether injected or monitored—leaves the existing record, including
its retry state, unchanged. This sentinel show is not
added to `queue.shows`, because it has no channel cursor or discovery process.

## Design decisions

Logic to be implemented in `downloader.py`

### Dataflow

1. for each show in config.json
    1. go to its YouTube videos page and identify the most recent episode IDs
    2. compare IDs with the show's existing `queue.json` records
    3. add unknown episodes with `download.status = "pending"`
    4. update `last_successful_check_at` only when that discovery pass succeeds
2. for each episode in `queue.json` with `download == "pending"`
    1. retrieve its title and publication date if not already recorded
    2. generate the `youtube-transcript.io` URL with the episode's YouTube video ID
    3. use one persistent Playwright browser context for the run, opening a fresh page to download each transcript
    4. validate that the downloaded file is non-empty, then move it to its final raw-transcript path
    5. record success or failure in the episode's `download` object
    6. mark `scrub.status = "pending"`; `summary` remains `not_ready` until scrubbing succeeds
    7. pause for a randomized interval from three seconds through `request_pause_seconds` before the next attempt

Manual injection bypasses the discovery loop entirely; it only creates the
same pending-download record that discovery would create.

### Download behavior

The selected v1 route is the YouTube Transcript browser UI validated in the source spike. The downloader uses the dedicated persistent Chromium profile, starts visibly by default, and waits for human intervention if the site's supported anonymous-login flow cannot complete automatically. It opens the browser context once per queue run, uses a fresh page for each episode, and closes the context in a `finally` block. It saves the browser download directly to the final raw-transcript path; it does not use the clipboard.

`request_pause_seconds` is the configurable upper bound for a randomized pause after each completed download attempt. The downloader samples a duration between three seconds and the configured value (ten seconds by default). This is a conservative operating assumption, not a claim about the provider's rate limit.

### What counts as a new YouTube episode

The downloader obtains the newest-first list of video IDs from a configured YouTube videos page. An episode is new when its `source_episode_id` is not already present in `queue.json` for that show.

`last_successful_check_at` remains useful: it tells the user whether a source has been checked recently and is only advanced after that show's discovery pass succeeds. It is **not** the de-duplication mechanism. The source spike found that YouTube's list does not expose a dependable publication date.

The downloader stops scanning once it has reached a previously known video ID, unless this is the first run. On the first run, `max_new_episodes_per_show` limits how much historical backlog is added.

### State ownership

`config.json` expresses intent and is edited by the user. `queue.json` is runtime state and is edited by the tool. Keeping them separate avoids a normal download run silently changing the user's configuration and makes a damaged or recreated queue an operational problem rather than a configuration change.

### Status model

There is one independent status for each downstream stage. The downloader owns only `download`: a successful download creates a pending scrub. `transcript_scrubber.py` owns `scrub`; a successful scrub creates a pending summary. This prevents source formatting from leaking into the summarizer while retaining the downloaded source record.

- `download.status`: `pending`, `succeeded`, or `failed`
- `scrub.status`: `not_ready`, `pending`, `succeeded`, or `failed`
- `summary.status`: `not_ready`, `pending`, `succeeded`, or `failed`

A failed download or scrub remains retryable on the next run of its respective command. `attempt_count`, `last_attempt_at`, and `last_error` make the failure inspectable without requiring a separate log database.


## Commands

```text
uv sync --group dev
uv run pytest -q
uv run python downloader.py --config config.json --discover-only
uv run python downloader.py --config config.json --add-episode "https://youtu.be/<video-id>"
uv run python downloader.py --config config.json
uv run python transcript_scrubber.py --config config.json
```

The first full downloader run will attempt the six currently queued episodes:
the latest three discovered for All-In and the latest three for 20VC. Follow it
with the scrubber command to make those raw transcripts available to the later
summarizer. The visible browser window may require intervention if the
provider's anonymous session has expired.
