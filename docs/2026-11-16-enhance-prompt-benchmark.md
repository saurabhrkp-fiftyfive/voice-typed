# F8 enhance prompt — 2026-11-16 benchmark

Why the prompt changed, how it was measured, and what to do about the model.

## What was actually running

`~/.config/secrets.env` holds both `OPENAI_API_KEY` and `GROQ_API_KEY`. The
OpenAI key is **out of credits** (`429 insufficient_quota`,
`credit_balance_exhausted`), so every F8 call since then burned a failed OpenAI
round trip (0.5–1.6 s) and was then rewritten by the **Groq fallback
`openai/gpt-oss-120b`** — not by `gpt-4o-mini`, which the config and README name
as the primary. The prompt had also been tuned against `gpt-4o-mini`.

The OpenRouter key is also spent (10.0 / 10.02 credits used), so no paid model
could be benchmarked for this change.

## Evidence

`journalctl --user -u voice-typed` holds 152 real F8 utterances, but the daemon
logs `text[:60]`, so only the first 60 characters of each input **and** output
survive. That is enough to score prompt *behaviour* (does it invent sections?)
on real input, and it is what the two real failures below were caught with.

Reproduced on real inputs, old prompt:

```
Context: The user needs to share a reply to Ian on the Rotenberg group's
communication channels, providing information on the migration timeline.
```

That is inside the prompt handed to the coding agent — third-person narration of
the speaker, produced by the `Context:` template, not by anything the speaker
said. Same template under pressure invented specifics wholesale: "Expected
output: a list of pods with their restart counts", "e.g. hours, cost, or
timeline", "Constraints: Estimate and missing-information list must be ready by
Friday".

## Benchmark

23 dictations reconstructed to full length from the real F8 heads (topics,
phrasing, technical terms and rambling style taken from the corpus; the tails
written by hand so there is something to lose). Cases 21–23 cover the
"message that still has to be written" pattern that F8 handles badly.

Scored deterministically, no judge model:

- **recall** — share of the source's content terms (fillers/stopwords removed,
  crude stemming) present in the output. Drops = details lost.
- **invent** — share of the output's content terms the source never contained.
- **drafted_instead** — output opens with a salutation, i.e. the model wrote the
  email the speaker only asked for.

All runs on `openai/gpt-oss-20b` (identical model across prompt variants, so the
comparison is prompt-only; `gpt-oss-120b` hit its 200k/day Groq token cap
mid-run):

| prompt | n | recall | worst case | invented | drafted instead | chars |
|---|---|---|---|---|---|---|
| old `ENHANCE_SYSTEM` | 16 | 0.809 | 0.579 | 0.448 | 1 | 419 |
| v2 (tighter, no labels) | 19 | 0.843 | 0.545 | 0.084 | 1 | 164 |
| v3 (coverage rules) | 20 | 0.982 | 0.636 | 0.006 | 1 | 177 |
| v3c (shipped 2026-11-16) | 16 | 1.000 | 1.000 | 0.000 | 0 | 168 |
| **v4 (structure restored)** | **23** | **0.977** | **0.895** | **0.004** | **0** | — |

v3c failures were all `429 TPM`, not quality.

v4 restores the `## Task / ## Context / ## Constraints / ## Expected output`
structure that was dropped in v3c — users asked for it back — while keeping
invention at zero. It scores `0.004` invented rather than `0.448` because the
forced template is gone: a heading appears only when the speaker's own words
fill it. Measured on `gpt-5.4-mini` (23 cases): headings used were `Task` 20/23,
`Context` 12/23, `Constraints` 6/23, `Expected output` 0/23 — the model never
invented the fourth section, which is exactly what the old prompt did on nearly
every call. 20/23 came out multi-line, short single-ask dictation stayed flat
("Analyze our repository."). p50 latency 1.33 s, p95 1.57 s.

Two concrete before/after cases:

- *"We recently moved everything to QA and deployed on to the QA server, but the
  frontend is still pointing to the old API…"* — v2 collapsed it to "Check the
  frontend build environment variables, fix the API URL", losing the QA story
  and the word QA. v3c keeps it.
- *"I have to form out a message for Ammar and Musa … keep it short and polite"*
  — v2 produced the message itself (`Hi Ammar and Musa, …`) and dropped the tone
  constraint. v3c keeps it a request: "I have to write a message to Ammar and
  Musa … Keep it short and polite."

## Choosing a model

Measured on this benchmark set, both current-generation cheap models are at
parity — the prompt is portable, the ranking is not. Per-call cost is a rounding
error at ~1.5k input + ~180 output tokens:

The `invented` figures below are from the model comparison run, which used the
v3c prompt (plain prose, no headings) so that heading tokens could not skew the
scores. For the shipped v4 prompt the comparable number is the 0.004 in the
table above — same model, different prompt; the two are deliberately not the
same run.

| model | $/M in | $/M out | ~cost per F8 | measured |
|---|---|---|---|---|
| **`gpt-5.4-mini`** (default) | 0.75 | 4.50 | $0.0016 | recall 0.987, invented 0.021, p50 1.32 s, vision ✓ |
| `gpt-5.4-nano` | 0.20 | 1.25 | $0.0005 | recall 0.990, invented 0.008, p50 1.39 s |
| `gpt-oss-120b` (Groq fallback) | 0.15 | 0.60 | $0.0003 | recall 0.982; free tier caps at 200k tokens/day ≈ 130 F8s |
| `gpt-4o-mini` (old default) | 0.15 | 0.60 | $0.0003 | 2024 model, no longer worth preferring |

`gpt-5.4-mini` is the default because it is the one that accepts screenshots
(F6/F7 grounding) and its measured quality is indistinguishable from nano on
this set. `gpt-5.4-nano` is a reasonable cheaper swap if vision is not needed.

## Reproduce

```bash
voice-typed preview "so um yesterday I pushed the fix for the empty labels..." --mode task
# prints the rewrite, then "— groq/openai/gpt-oss-120b · 1.2s · mode=task" on stderr
```
