# Podcast Summarizer — raw transcript normalization Plan and Design Notes

`transcript_scrubber.py` receives a downloaded raw `.txt` transcript and writes
a separate scrubbed `.txt` file. The raw file remains the source record; the
scrubbed file is the only transcript passed to the summarizer.

The first implementation supports the two observed YouTube Transcript formats:

1. A timestamp on its own line, followed by one or more spoken-text lines.
2. A timestamp at the start of a spoken-text line, with the sentence continued
   across later lines.

For both formats, the scrubber will:

- Remove timestamp tokens only when they appear at the beginning of a line,
  supporting both `MM:SS` and `HH:MM:SS`.
- Collapse line breaks and repeated whitespace into ordinary single spaces.
- Preserve `>>` speaker-turn markers exactly and begin each marker on a new
  line, because they carry useful discussion structure even though they do not
  identify a speaker.
- Preserve all non-formatting text; it will not correct transcription errors,
  infer speaker names, or rewrite prose.

The transformation must be deterministic and idempotent: the same raw input
produces the same scrubbed output, and scrubbing an already scrubbed file does
not change it further.

The queue will gain a separate `scrub` lifecycle stage: a successful download
creates `scrub: pending`; a successful scrub creates `summary: pending`. A
scrub failure is recorded and retried later without re-downloading the raw
transcript.

Suggested local layout for this increment:

```text
transcripts/
  raw/<episode filename>.txt
  scrubbed/<episode filename>.txt
```

The implementation will include fixture-based unit tests for both timestamp
formats, timestamp-free text, speaker markers, and idempotence.

Use `python transcript_scrubber.py --config config.json --force` to recreate
existing scrubbed transcripts from their raw source files after changing the
normalization rules.
