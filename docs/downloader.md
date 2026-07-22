# Podcast Summarizer — Downloader Plan and Design Notes

This is a living record of decisions and system-design thinking. 

## Purpose

The downloader component discovers episodes, records newly seen source IDs/URLs in the queue, and retrieves their transcripts.

For context, see 'Working architecture hypothesis' section in `plan.md`. 

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

1. A set of downloaded transcripts in `transcripts/`. Each file is named `<YYYYMMDD>-<show-id>-<video-id>-<episode-slug>.txt`, for example:

```
20260717-all-in-PHL1j2ti420-more-trillion-dollar-ipos-anthropic-3t-zucks-price-war.txt
```

The source video ID makes a filename unique even if titles change or two episodes have the same title. The title slug is for human recognition only.


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
      "transcript_path": "transcripts/20260717-all-in-PHL1j2ti420-more-trillion-dollar-ipos-anthropic-3t-zucks-price-war.txt",
      "download": {
        "status": "succeeded",
        "attempt_count": 1,
        "last_attempt_at": "2026-07-22T18:31:12Z",
        "completed_at": "2026-07-22T18:31:25Z",
        "last_error": null
      },
      "summary": {
        "status": "pending",
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
    3. use the persistent Playwright browser to download the transcript
    4. validate that the downloaded file is non-empty, then move it to its final path
    5. record success or failure in the episode's `download` object
    6. pause for a randomized interval from three seconds through `request_pause_seconds` before the next attempt

### Download behavior

The selected v1 route is the YouTube Transcript browser UI validated in the source spike. The downloader uses the dedicated persistent Chromium profile, starts visibly by default, and waits for human intervention if the site's supported anonymous-login flow cannot complete automatically. It saves the browser download directly to the final transcript path; it does not use the clipboard.

`request_pause_seconds` is the configurable upper bound for a randomized pause after each completed download attempt. The downloader samples a duration between three seconds and the configured value (ten seconds by default). This is a conservative operating assumption, not a claim about the provider's rate limit.

### What counts as a new YouTube episode

The downloader obtains the newest-first list of video IDs from a configured YouTube videos page. An episode is new when its `source_episode_id` is not already present in `queue.json` for that show.

`last_successful_check_at` remains useful: it tells the user whether a source has been checked recently and is only advanced after that show's discovery pass succeeds. It is **not** the de-duplication mechanism. The source spike found that YouTube's list does not expose a dependable publication date.

The downloader stops scanning once it has reached a previously known video ID, unless this is the first run. On the first run, `max_new_episodes_per_show` limits how much historical backlog is added.

### State ownership

`config.json` expresses intent and is edited by the user. `queue.json` is runtime state and is edited by the tool. Keeping them separate avoids a normal download run silently changing the user's configuration and makes a damaged or recreated queue an operational problem rather than a configuration change.

### Status model

There is one independent status for each downstream stage. In this increment, the downloader owns only `download`; it initializes `summary` so the later summarizer can use the same queue without reinterpreting a vague `processed` flag.

- `download.status`: `pending`, `succeeded`, or `failed`
- `summary.status`: `not_ready`, `pending`, `succeeded`, or `failed`

A failed download remains retryable on the next run. `attempt_count`, `last_attempt_at`, and `last_error` make the failure inspectable without requiring a separate log database.


## Preconditions before coding

1. **Complete, 2026-07-22:** The 20VC videos page exposed ordinary
   `/watch?v=<video-id>` links, including the supplied representative video.
   The browser diagnostic successfully exercised its transcript Copy and
   Download controls for `HoRaqNWKcpM`.
2. Decide the initial command contract. Recommended: `python downloader.py
   --config config.json`; it discovers and downloads in one run, while
   `--discover-only` allows inspection of the queue without browser work.
3. Add an empty `config.json` based on the contract above and an empty
   `queue.json` based on its initial-state example. The downloader should
   create `transcripts/` if absent and write JSON by replacing a temporary file
   in the same directory, so an interrupted run does not leave a partial queue.
