# Podcast Summarizer — downloader Plan and Design Notes

This is a living record of decisions and system-design thinking. 

## Purpose

The downloader component discover episodes since the last successful check, add new source IDs/URLs to the queue, and retrieve transcripts. 

For context, see 'Working architecture hypothesis' section in `plan.md`. 

## Input / Output contract

**Input:**

`config.json` stores for each show:
- Show: e.g., "all-in-with-chamath-jason-sacks-friedberg")
- Youtube_show_page: 'https://www.youtube.com/@allin/videos'
- last_checked: date the show page was last checked for new episodes, e.g., "20260722"

**Output:**

1. A set of downloaded transcripts in `transcripts/`. Each file is named `<YYYYMMDD><Show Name><Episode title>.txt`, e.g.:

```
202607017-all-in-with-chamath-jason-sacks-friedberg-more-trillion-dollar-ipos-anthropic-3t-zucks-price-war-china-ends-open-source-trump-accounts.txt
```

2. `queue.json` which stores for each downloaded transcript:
- youtube_ID: e.g., "9IMwRIei-Xc" 
- download: status of successful downloading, i.e., {pending, success, failure} 
- processed: status of successful summarization, i.e, {pending, completed}


## Design decisions


## Implementation notes

Logic to be implemented in `downloader.py`

### Dataflow

1. for each show in config.json
    1. go to its Youtube page and identity the most recent episodes
    2. append new episodes are not `queue.json` with `download = "pending"`
2. for each episode in `queue.json` with `download == "pending"`
    1. go to programmatically generated playwright URL with youtube video ID
    2. download transcript
    3. wait for respectful amount of time 
