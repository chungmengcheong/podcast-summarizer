# Spike - Transcripton sources  

## Potential Sources

The following sources were considered: 

**Podscripts**

All-in show page
https://podscripts.co/podcasts/all-in-with-chamath-jason-sacks-friedberg/

All-in example episode
https://podscripts.co/podcasts/all-in-with-chamath-jason-sacks-friedberg/more-trillion-dollar-ipos-anthropic-3t-zucks-price-war-china-ends-open-source-trump-accounts

20VC show page

**YouTube**

All-in show page
https://www.youtube.com/playlist?list=PLn5MTSAqaf8peDZQ57QkJBzewJU1aUokl

All-in example episode
https://www.youtube.com/watch?v=PHL1j2ti420&list=PLn5MTSAqaf8peDZQ57QkJBzewJU1aUokl&index=2

Notes:
- if youtube transcription is too difficult to parse, we can use the show ID and put into https://www.youtube-transcript.io/

**Podchaser**

All-in show page
https://www.podchaser.com/podcasts/all-in-with-chamath-jason-sack-1057128

All-in example episode
https://www.podchaser.com/podcasts/all-in-with-chamath-jason-sack-1057128/episodes/the-trillion-dollar-industries-301653064/transcript

Notes:
- full transcripts seems to be only available for shows published greater than 7 days ago

## Source spike findings — All-In

**Scope and method:** Tested the show and example-episode pages above on 2026-07-22. The question was whether the application can use an ordinary HTTP client and HTML parser, rather than browser automation, to discover new episodes and retrieve a usable transcript. Findings for All-In are evidence, not yet a guarantee of 20VC coverage or long-term provider stability.

| Source | Discovering new episodes | Navigating and extracting a transcript | Transcript quality | v1 assessment |
|---|---|---|---|---|
| Podscripts | **Easy.** The server-rendered show page contains episode links, titles, and explicit `Episode Date` values in newest-first order. It also exposes pagination (20 pages observed). | **Easy.** The linked episode page returns its full, timestamped transcript in the ordinary HTML response. A Python HTTP client plus HTML parser is sufficient; no Playwright is needed. | **Usable for summarization.** The tested transcript has frequent timestamps and coherent content, but no speaker labels and visible automatic-transcription errors (for example, names). It is not strong evidence for precise speaker attribution. | **Not viable as the sole v1 source** because it does not carry 20VC. |
| YouTube | **Moderate.** The playlist response contains video IDs and titles, but not a dependable published date for each item. The video page exposes a precise `publishDate`, so discovery would require parsing the playlist and then examining candidate video pages. This relies on YouTube's embedded page data, not a stable public API. | **Weak directly.** The video response advertised an auto-generated English caption track, but the rendered player reported captions unavailable and the associated timed-text request returned an empty response. A direct HTTP path is possible in principle, but it is undocumented and best-effort. | **Not assessable from YouTube directly** because no captions were returned. When available, the only advertised track was auto-generated and should be assumed to lack reliable speaker attribution. | **Direct captions: no.** YouTube plus YouTube Transcript's authenticated API is a viable path pending a controlled transcript-quality and cost test. |
| Podchaser | **Moderate in a browser; poor for a simple HTTP client.** The rendered show page exposed recent episode links and dates, but a normal HTTP request was blocked by CloudFront. Podchaser also offers an authenticated API. | **Poor for the tested episode.** The public transcript page rendered episode metadata and a Transcript tab, but no transcript text. Podchaser's documentation says programmatic transcript retrieval requires a Plus or Pro plan. | **Not assessable on the public page.** Its API documentation describes timestamped transcript JSON, but its example has `speaker: null`; this does not solve speaker attribution. | **Do not use for v1.** It would introduce browser automation or a paid, authenticated API before we have established need. |

### Superseded decision: Podscripts-only v1

The earlier Podscripts-only proposal is no longer a v1 option because it cannot cover 20VC. The following flow remains a useful comparison point for a future provider that offers similarly accessible static pages:

```text
source adapter
  → ordinary HTTP GET of a configured Podscripts show page
  → parse episode URL, title, and published date from the returned HTML
  → compare published date with the show's last successful check
  → de-duplicate discovered episodes using the stable episode URL
  → ordinary HTTP GET of each queued episode page
  → parse and save the full transcript text
```

This is HTML parsing, not browser-DOM automation: Python receives the page response and parses its HTML into a document tree. It avoids Playwright and the operating complexity of a persistent browser process.

The adapter should scan a small overlap before the last successful check and de-duplicate by episode URL. The date makes discovery efficient; the URL makes it safe if a source republishes, reorders, or changes a displayed date.


## YouTube re-evaluation

### 1. Discovering new episodes

**Yes, programmatically, with a caveat.** The All-In playlist page exposes the current ordered videos as `/watch?v=<video-id>&list=...&index=...` links. The ordinary HTTP response also contains the video IDs in embedded page data. The latest tested entry was `9IMwRIei-Xc`.

The playlist does **not** provide a dependable publication date in the returned list. A downloader can either:

- treat the newest ordered video IDs as the discovery signal and stop once it reaches an ID already in the local episode queue; or
- fetch each newly discovered video's ordinary HTML page and parse its embedded `publishDate` for a precise date.

The first approach is simpler for periodic runs. It does not need Playwright, but it does depend on an undocumented YouTube page-data structure that may change. Video IDs, not a `last checked date`, should be the primary incremental checkpoint for this source.

### 2. Extracting the video ID

**Yes.** The downloader can extract the `v` query parameter from the playlist's episode link, for example:

```text
/watch?v=9IMwRIei-Xc&list=PLn5MTSAqaf8peDZQ57QkJBzewJU1aUokl&index=1
        ^^^^^^^^^^^
```

It should store the bare value (`9IMwRIei-Xc`) as the source episode ID and construct the canonical URL when needed.

### 3. Obtaining a transcript from YouTube Transcript

#### API route

The service documents a better production interface: `POST https://www.youtube-transcript.io/api/transcripts` with an array of up to 50 video IDs, a Basic API token generated from the user's profile, and a limit of five requests per ten seconds. The token and pricing are account-specific. The production downloader should use this API—not Playwright against the web form—if we choose this provider.


### Browser UI route: corrected finding

The paid API is not required for a hobby-project experiment. The service's browser UI works through a **dedicated persistent Chromium profile**:

1. On the first visible run, the user clicks the site's supported **Login Anonymously** control.
2. The service enables the transcript controls for the requested video.
3. A later fresh run using the same profile successfully reached the enabled **Copy Transcript** control without another click.

This is a viable low-cost path for a personal tool, provided that the first-run bootstrap is manual and the scheduled job reuses the local profile directory. It is not an API-quality integration: the browser UI, anonymous-session lifetime, and page controls are all external dependencies that can change.

The local diagnostic is `tests/check_youtube_transcript_access.py`. It activates the site's supported **Login Anonymously** control once when it is present; if that does not complete, it leaves the visible window open for up to two minutes of human intervention.

**Control test, 2026-07-22:** For `PHL1j2ti420`, the script successfully exercised both controls. Copy returned 104,336 characters and the default `.txt` download returned 112,936 characters. Both included `>>` speaker-turn markers. The longer downloaded file is consistent with the modal's default **Include Timing** setting.

For the product, prefer the Download route over browser clipboard access: it produces a file the tool can save directly and retains timing information. The Copy path remains a useful lightweight smoke test.

### Reference evidence

- The tested [Podscripts All-In show page](https://podscripts.co/podcasts/all-in-with-chamath-jason-sacks-friedberg/) lists 399 transcribed episodes, newest-first, with explicit episode dates; the tested [episode page](https://podscripts.co/podcasts/all-in-with-chamath-jason-sacks-friedberg/more-trillion-dollar-ipos-anthropic-3t-zucks-price-war-china-ends-open-source-trump-accounts) contains the timestamped transcript in page HTML.
- The tested [YouTube playlist](https://www.youtube.com/playlist?list=PLn5MTSAqaf8peDZQ57QkJBzewJU1aUokl) and [episode video](https://www.youtube.com/watch?v=PHL1j2ti420) supplied embedded video metadata, but the tested video did not yield caption text.
- The tested [Podchaser show page](https://www.podchaser.com/podcasts/all-in-with-chamath-jason-sack-1057128) exposed episode metadata in a rendered browser page, while the tested [transcript page](https://www.podchaser.com/podcasts/all-in-with-chamath-jason-sack-1057128/episodes/the-trillion-dollar-industries-301653064/transcript) did not render transcript text. Podchaser's [transcript API documentation](https://api-docs.podchaser.com/docs/guides/getting-transcripts-individual-episode/) says API transcript access requires a Plus or Pro plan.

## Implications for summary design

The summary prompt must avoid asserting who said a point unless the transcript itself provides clear context. It can report a disagreement as a disagreement in the discussion, but should qualify uncertain attribution. Speaker diarization is explicitly out of scope for v1.

