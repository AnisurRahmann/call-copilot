# Project Knowledge

Living record of decisions, invariants, and rationale for `call-copilot` (`rec`).
Append-only under each dated entry; never rewrite history here.

---

## Decisions

### 2026-08-10 — Summarisation becomes an opt-in, BYOK feature

`rec` was originally explicit about *not* summarising: the README listed "Not a
meeting summarizer (just the transcript)" as a non-goal. Step 3 reverses that,
narrowly.

**What changed:** `rec` can now turn a transcript into a `summary.md` via a
map/reduce pass across three model tiers, using the user's own API key.

**What did NOT change — and is the whole trust model:**

- **Offline by default.** A fresh install never makes a network call and never
  prompts. With no provider configured, `rec` behaves exactly as it always has.
- **Keys live in environment variables only.** `config.json` stores the *name*
  of the env var, never the key. `rec diagnose` and `session.json` are safe by
  construction.
- **`transcript.md` is never modified by summarisation.** The summary lives only
  in `summary.md`. The transcript stays a verbatim record; `search_transcripts`
  can never return model-generated text because model-generated text never enters
  the indexed file.
- **The MCP server stays read-only.** `get_summary` reads `summary.md` off disk.
  It cannot generate, mutate, or make a network call.

**Why BYOK and tiered:** 70–80% of the tokens go to a cheap model (e.g.
`glm-4.7-flash`) per chunk; the expensive model (e.g. `glm-5`) sees a single
condensed reduce pass. Real cost lands well under a cent per meeting. The user
pays their own metered rate to their own provider — `rec` never resells or
proxies.

**Spec:** [`docs/specs/STEP3_SUMMARIES.md`](docs/specs/STEP3_SUMMARIES.md).

---

## Invariants

These hold across the codebase and are pinned by tests. Break one and a test
should fail.

- **`transcript.md` is write-once by transcription.** Summarisation, indexing,
  and the MCP server read it; nothing appends to it. Re-transcription rewrites it
  wholesale.
- **`index.py` indexes only `TRANSCRIPT_FILENAME`.** Never `summary.md`.
- **The MCP server imports no write path** — no `recorder`, `transcriber`,
  `audio_check`, `web`, `summarize`, or `providers`. (Guarded by source checks in
  `test_mcp_server.py` and `test_layering_guard.py`.)
- **The web layer reaches audio/model/sqlite only through core modules**, never
  by importing `audiotap`/`faster_whisper`/`sqlite3` directly. (AST guard in
  `test_layering_guard.py`.)
- **Console logging is on stderr.** The MCP server owns stdout (JSON-RPC), so no
  `rec` code writes to stdout via logging.
- **Never log transcript/chunk/summary text.** Ids, counts, token numbers, model
  names, durations only. The global file handler is at DEBUG.
