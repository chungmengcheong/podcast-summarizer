# Podcast Summarizer — transcript summarizer Plan and Design Notes

## Purpose

The summarizer component uses a locally installed AI CLI to generate summaries from normalized transcripts. 

For context, see 'Working architecture hypothesis' section in `plan.md`. 

## Input Contract

`config.json` specifies the locally installed AI CLI tool. Default choices are codex or claude code. 

Normalized transcripts

## Output Contract

Each episode summary is a Markdown file with:

- Episode metadata: show, title, publication date, source URL, and transcript source.
- Executive takeaway: summary of the main theses/questions explored and conclusions.
- Key points: the top 3-7 news, claims, and conclusions synthesized as:
  - Point 1: news, claim, or conclusion.
    - Logic chain: A (with supporting evidence) → B (with supporting evidence) → claim or conclusion (with supporting evidence) → implications X and Y.
    - Areas of notable disagreement between podcast guests: the differing claim, logic, or evidence.
- Hot takes: top 1-3 novel but unsubstantiated observations. Label each explicitly as an inference and state the discussion evidence or reasoning that prompted it.

The exact prompt and template are stored in editable local files so the user can tune the balance of business, technical, or other detail.

## Logic flow

For each episode that has `summary.status == "pending"` 
1. Send prompt and normalized transcript to CLI tool
2. Save the summarized transcript

## Prompt for CLI AI Tool

**Reference:**

Incorporate the following guidelines as helpful ito the final prompt

```
### Judgment rules

- Prioritize signal over completeness, i.e., fewer, sharper bullets that captures the gist of the podcast rather than encapsulating every detail. 
- Omit items that do not change the summary in a meaningful way.
- Do not force a coherent narrative when the logic chain, claim, and implication is  contradictory.
- Do not force a implication unless it is reasonably supported.
- Do not create fake certainty out of sparse evidence.
- Keep the synthesis evidence-centric. 
    - Evidence should include actual observed facts, patterns, or developments that support the view.
    - Evidence against should include actual observed facts or patterns that cut against the view.
- If evidence is mixed, say so plainly.
- Distinguish between:
  - factual datapoints
  - first-hand operator observations
  - interpretation
  - speculation
- Evidence should be drawn from the transcription and not from any other source 


## Writing Style

Write like a smart operator explaining the news to another smart operator.

Requirements:
- Use plain English.
- Prefer short sentences.
- Prefer concrete nouns and verbs over abstract nouns.
- Be concise without becoming cryptic.
- Be analytical without sounding like a memo or slide deck.
- Do not use hype.
- Do not use consultant, VC, or strategy jargon unless it is clearly the simplest term.
- If a sentence sounds impressive but not natural, rewrite it more simply.
- Prefer sharp claims over mushy summaries, but do not overstate the evidence.

Before writing each section, ask:
- What is the simplest way to say this?
- Would a smart reader say this out loud in conversation?
- Can I replace an abstract noun with a concrete verb?
- Can I cut this sentence by 30% without losing meaning?
- Is this observation sharp enough to matter for capital allocation, company-building, or product design?

Avoid phrases like:
- "moat formation"
- "narrative support"
- "machine-readable execution"
- "relationship-bearing judgment"
- "opening salvo"

Prefer:
- "who owns the workflow" over "workflow control"
- "can actually do the work" over "permission to act"
- "investors seem to want" over "public markets are demanding proof"
- "this suggests" over "what it may imply is"

### Writing examples

Bad:
- The equity market may punish AI credibility gaps faster than it rewards generic exposure.

Better:
- Investors seem quicker to punish weak AI stories than to reward vague AI positioning.

Bad:
- The application moat is moving away from raw model quality and toward workflow control, proprietary context, and permission to act.

Better:
- Better models alone are not enough. The stronger products are the ones that already own the workflow, hold useful context, and can actually do things inside it.
```