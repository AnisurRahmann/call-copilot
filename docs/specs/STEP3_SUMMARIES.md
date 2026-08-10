# Step 3 — Summaries, BYOK, Tiered Routing

> Commit this to `docs/specs/STEP3_SUMMARIES.md`. It is self-contained and supersedes every earlier Step 3 document. It already incorporates the code review that found the `KeyboardInterrupt` rollback bug, the `_finish_session` early-return problem, and the CliRunner test-harness gaps — those are not separate files to chase.

---

## Scope

Three commits, in this order. Do not invert them.

| Commit | Contents | Ships independently |
|---|---|---|
| **A — abort rollback fix** | `except BaseException` in `_transcribe_session`, one regression test | Yes. This is a bug in shipped code today, unrelated to summaries. |
| **B — summarisation core** | Providers, pricing, chunking, `summarize.py`, `rec summarize`, `--dry-run`, `summary.md`, prompt templates, `get_summary`, `has_summary` | Yes. Flag-driven only, no interactive prompt. |
| **C — interactive prompt** | Signal handler, `prompt_yes_no`, `_is_interactive`, `summarize.auto`, flags on `start` and `stop`, README + decision log | Needs B. |

B is the feature. C is a prompt. C looks more like progress than it is.

---

## Review first — before writing any code

Read `src/rec/` in full — `cli.py`, `session.py`, `config.py`, `transcriber.py`, `formatter.py`, `index.py`, `mcp_server.py`, `log.py`, `envcheck.py` — plus `tests/unit/conftest.py` and `tests/unit/test_cli.py`.

Then report, before implementing:

1. Anything here that will break at runtime against the actual code — wrong function names, wrong config shape, wrong session lifecycle assumptions.
2. Any place this spec contradicts itself. Flag it; don't silently pick one.
3. The exact `SessionMeta` change needed, given that `load_meta` filters unknown keys into `extra`.
4. Whether `_finish_session`'s exit paths match §7's description. That section was written from a review, not from reading the file.

Blockers first. Nits never.

---

## Hard constraints

- **No new runtime dependency.** HTTP is stdlib `urllib.request` + `json`. One JSON POST per call, no streaming, no async. If you think you need `httpx`, write the justification and stop.
- **Offline by default.** A fresh install must never make a network call and must behave exactly as it does today.
- **API keys live in environment variables only.** `config.json` stores the *name* of the env var, never the key. This makes `rec diagnose` and `session.json` safe by construction. Do not add an `api_key` field "for convenience."
- **Never log transcript text, chunk text, or summary text.** Ids, counts, token numbers, model names, durations only. The global file handler is at DEBUG, so the rule is enforced by never passing text into a `log.*` call.
- **`transcript.md` holds the transcript and nothing else.** No summary block, no marker, no footer pointer. Ever. See §10.
- **The MCP server stays read-only.** It must not import `summarize` or any provider module, must not generate, must not mutate, must not make a network call.
- **All tests run offline.** No network, no audio device, no model download.
- **macOS 14.2+, Python 3.11+.** No cross-platform work.

---

## Commit A — abort rollback fix

`_transcribe_session` wraps `_transcribe_session_inner` in `except Exception` to roll status back to `RECORDED` on failure. `KeyboardInterrupt` is a `BaseException`, so Ctrl+C during transcription skips the rollback, leaves `session.json` at `STATUS_TRANSCRIBING` permanently, and poisons `rec status` via `_transcribing_session`.

```python
try:
    _transcribe_session_inner(...)
except BaseException:
    # Includes KeyboardInterrupt and SystemExit. Roll back, then re-raise so
    # exit semantics are unchanged.
    try:
        session.update_meta(session_id, status=session.STATUS_RECORDED)
    except BaseException:
        pass  # a wedged status is bad; masking the original abort is worse
    raise
```

Test: monkeypatch `transcriber.transcribe` to raise `KeyboardInterrupt`; assert status is `RECORDED` and the exit code is non-zero.

Own commit. Do not bundle into B.

---

## Commit B — summarisation core

### Module layout

```
src/rec/
  summarize.py            # orchestration: chunk → map → reduce → write
  chunking.py             # token-aware transcript splitting
  providers/
    __init__.py           # registry: name -> Provider factory
    base.py               # Provider protocol, Completion, ProviderError
    openai_compat.py      # /chat/completions — GLM, DeepSeek, OpenRouter, LM Studio, vLLM
    anthropic_compat.py   # /v1/messages — Anthropic, and Z.ai's Anthropic endpoint
    gemini.py             # generativelanguage endpoint
    ollama.py             # http://localhost:11434/api/chat
    pricing.py            # static price table + cost math
  prompts/
    default.md
    standup.md
    client-call.md
    architecture-review.md
    interview.md
```

`prompts/*.md` ship as package data — add them to `pyproject.toml` so the wheel and the Homebrew sdist both carry them. A missing template on a clean install is a shipping bug; pin it with a test that loads every built-in template by name.

User templates in `~/.config/rec/prompts/*.md` override built-ins by filename stem.

### Provider abstraction

```python
# providers/base.py
@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None      # None when the model isn't in the price table

class Provider(Protocol):
    name: str
    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        temperature: float = 0.2,
        timeout: float = 300.0,
    ) -> Completion: ...
```

- `tokens_in`/`tokens_out` come from the provider's reported `usage` block. If the provider reports nothing (some local endpoints), fall back to the char/4 estimate **and mark the cost line as estimated**. Never present a guess as measured.
- Retries: 3 attempts, backoff 1s / 4s / 10s, only on 429/5xx/timeout. Never retry a 401 or 400 — surface those immediately with the provider's message.
- Timeout default 300s. A 60s default will kill long GLM generations; this is a known failure, not a tuning preference.
- `ProviderError` carries status code and provider message. `cli.py` renders it as one human line, never a traceback.

### GLM configuration — the maintainer's path

```json
{
  "summarize": {
    "provider": "glm",
    "api_key_env": "ZAI_API_KEY",
    "confirmed_network": false,
    "auto": "ask",
    "prompt_timeout_s": 60,
    "tier1_model": "glm-4.7-flash",
    "tier2_model": "glm-4.7",
    "tier3_model": "glm-5",
    "base_url": null
  }
}
```

- `provider: "glm"` resolves to the **OpenAI-compatible** transport at `https://api.z.ai/api/paas/v4`. Mainland accounts override `base_url` to `https://open.bigmodel.cn/api/paas/v4`.
- Key resolution: `--api-key-env` flag → `summarize.api_key_env` → first non-empty of `ZAI_API_KEY`, `GLM_API_KEY`, `ZHIPU_API_KEY`. None set → error naming the exact variable to export.
- Also register a `glm-anthropic` preset pointing the Anthropic transport at `https://api.z.ai/api/anthropic`. **Document in the error path that this endpoint takes a Coding Plan key, a different credential from the standard API key** — mixing them produces a 401 that looks like a bad key.

**GLM quirks the transport must handle:**

1. **Thinking is billed at the output rate.** Send `"thinking": {"type": "disabled"}` on every Tier 1 map call. Map passes are extraction, not reasoning — paying reasoning rates forty times over is the exact failure this tiering exists to prevent. Tier 3 may leave it on.
2. **If a response carries `reasoning_content`, ignore it** for the summary body but still count its tokens in `tokens_out`, or the cost line under-reports.
3. **Never mangle the model string.** `glm-5.2[1m]` and similar pass through verbatim.
4. **Latency.** Servers are primarily in China; 100–200ms base latency plus long generations. This is why the timeout is 300s.

### Price table (`providers/pricing.py`)

USD per 1M tokens, input/output. Put `PRICING_UPDATED = "2026-08-10"` at the top and print a note when the table is older than 180 days.

| Model | In | Out |
|---|---|---|
| glm-4.7-flash | 0.06 | 0.40 |
| glm-4.7-flashx | 0.07 | 0.40 |
| glm-4.7 | 0.40 | 1.75 |
| glm-5 | 0.60 | 1.92 |
| glm-5.1 | 0.95 | 2.99 |
| glm-5.2 | 1.40 | 4.40 |

Add the Anthropic, Gemini and DeepSeek models used by the other presets.

Unknown model → `cost_usd = None` → the line reads `cost unknown (model not in price table)`. **Never print `$0.00` for a model you can't price.** Only Ollama gets a true zero.

Estimates print to two significant figures — `~$0.004`, not `~$0.0041`. Four decimals implies confidence the table can't support.

### Tiered routing — mandatory

- **Tier 1 — 70–80% of all tokens.** Per-chunk map pass: cleanup, speaker attribution refinement using the `[Mic]`/`[System]` labels, extraction of candidate decisions, action items, open questions. `glm-4.7-flash`, Ollama local, or Gemini Flash.
- **Tier 2 — optional.** Fires only when Tier 1 output exceeds the reduce budget (default: > 12k estimated tokens). Consolidates map output into fewer, denser blocks. `glm-4.7` or Haiku. Below budget, zero calls, and the cost line shows `0 tier-2`.
- **Tier 3 — ≤5% of tokens, exactly one call.** Reduce pass over Tier 1/2 output only. `glm-5` or Sonnet-class.
- **A raw full transcript must never reach Tier 3.** Construct the reduce input only from map/consolidate outputs, and pin it with a test asserting the Tier 3 payload contains no chunk text.

### Cost line

```
summary: $0.0041 — 14 tier-1 calls (glm-4.7-flash), 0 tier-2, 1 tier-3 call (glm-5), 38.2k in / 4.1k out, 47s
```

Ollama prints `$0.00`. Unmeasured runs print `~$0.004 (estimated — provider reported no usage)`. One test asserts the format; it's a UX commitment.

### Chunking (`chunking.py`)

- Token estimate is `len(text) / 4`. No tokenizer dependency. **Sizing only** — real cost always comes from reported usage.
- Target 6,000 estimated tokens per chunk, hard ceiling 8,000.
- **Split only on transcript line boundaries.** Never mid-line.
- **Both transcript line formats must parse.** Merged: `[System] [00:12] text`. Single-source: `System [00:00] text` — label before the timestamp, no brackets. A parser handling only one silently produces unlabelled chunks on half the sessions.
- Overlap: carry the last 3 lines of chunk N into chunk N+1 so a decision spanning a boundary isn't lost.
- Must survive a 3-hour transcript.

### Prompt templates

Each is a markdown file with a system block and a user block separated by `---`, and two placeholders: `{{transcript_chunk}}` (map) and `{{map_output}}` (reduce). Every template needs both sections.

- `default.md` — decisions, action items with owners, open questions, one-paragraph narrative.
- `standup.md` — what shipped, what's blocked, what's next, per person.
- `client-call.md` — requirements stated, scope changes, commitments made by each side, follow-ups. Bias toward anything that could become a scope dispute.
- `architecture-review.md` — options considered, decision, rationale, rejected alternatives, open risks. Should read like an ADR.
- `interview.md` — questions asked, how they were answered, moments where the answer was weak. This serves the self-review use case, which is the strongest differentiated story in the product. Make it genuinely useful, not a generic recap.

Every template instructs the model to preserve `[MM:SS]` timestamps on decisions and action items so the summary stays citable back to the transcript.

### Session metadata

Add to `SessionMeta`:

```python
summary: dict = field(default_factory=dict)
```

Populated on success:

```json
{
  "generated_at": "2026-08-10T14:22:07",
  "template": "default",
  "provider": "glm",
  "models": {"tier1": "glm-4.7-flash", "tier2": null, "tier3": "glm-5"},
  "calls": {"tier1": 14, "tier2": 0, "tier3": 1},
  "tokens": {"in": 38210, "out": 4102},
  "cost_usd": 0.0041,
  "cost_estimated": false,
  "wall_clock_s": 47.3
}
```

No key, no key name, no text.

### CLI (commit B)

```
rec summarize <id> [--template NAME] [--template-file PATH]
                   [--provider NAME] [--tier1 MODEL] [--tier2 MODEL] [--tier3 MODEL]
                   [--dry-run] [--force] [--yes]
```

- Partial ids resolve through the existing `_resolve_session_id`.
- `--dry-run` prints chunk count, estimated tokens per tier, and estimated cost, with **zero network calls**. This is the acceptance demo and the thing that earns trust before a first paid run.
- `--force` overwrites an existing `summary.md`. Without it, an existing summary is an error, not a silent overwrite.
- **First network run prompts once:** `This sends transcript text to api.z.ai. Continue? [y/N]`. On confirm, set `summarize.confirmed_network: true`. `--yes` skips it. Ollama and any `localhost`/`127.0.0.1` base URL never prompt.
- `rec summarize` bypasses the envcheck gate, same reasoning as `rec mcp` — summarising has nothing to do with audio capture and must work anywhere transcripts were copied.

### Failure handling

- A chunk that fails all retries does not abort the run. Mark it `[chunk N unavailable]` in the map output and continue. The reduce pass sees the gap; the final summary carries a one-line note.
- A failed reduce writes `summary.partial.md` containing the map output, so tokens already paid for aren't lost.
- Any run that made ≥1 successful call prints the cost line even on failure. You paid for it; you should see it.

### MCP (commit B)

- **`get_summary(session_id)`** — new tool. Reads `summary.md` and returns it, or returns "no summary yet — run `rec summarize <id>`". Never generates, never mutates, never calls the network.
- **`has_summary: bool`** added to the dicts built by `list_sessions` and `get_session`. Without it an agent has no way to know a summary exists and will never call `get_summary` — the tool would ship dead. Computed from `summary.md` existing on disk.
- **Update the `list_sessions` docstring.** It enumerates its returned fields; an undocumented field is invisible to the model.
- Extend the existing read-only guard test to assert `mcp_server` never imports `summarize` or `providers`.

---

## Commit C — the interactive prompt

### Behaviour

After a recording is transcribed, `rec` asks:

```
Transcript: ~/.local/share/rec/sessions/2026-08-10_14-30-00/transcript.md (4,182 words)

Summarise this meeting?
  14 chunks · ~38k tokens · est. ~$0.004 · glm-4.7-flash → glm-5
[Y/n]
```

Enter means yes. The estimate comes from the `--dry-run` path — zero network calls to produce it. Showing the number before asking is the point: default-yes only earns trust if the user can see what yes costs.

On completion, print the summary path and the real cost line.

### Where it hooks

**Only at the TRANSCRIBED exit of `_finish_session`** — after `transcript.md` is written and `session.json` is committed.

`_finish_session` has early returns before any transcript exists (no-WAV, all-silent → `STATUS_SILENT`). Hooking the tail of the function fires the prompt on a silent session, where there is nothing to summarise. Guard on the post-condition, not on reaching the end:

```python
if session.transcript_path(sid).exists() and meta.status == session.STATUS_TRANSCRIBED:
```

Both conditions — either alone can be true where the other isn't.

### The SIGINT recipe

The codebase has no `signal.signal` call anywhere; `_run_live_recording` uses `try/except KeyboardInterrupt` around the Live loop, which stops working the moment execution leaves that loop.

```python
import signal

_stop_requested = False

def _install_stop_handler() -> None:
    """First SIGINT asks the recording loop to stop; the next one aborts."""
    def _handler(signum, frame):
        global _stop_requested
        _stop_requested = True
        # Restore Python's default: a second Ctrl+C raises KeyboardInterrupt
        # in the main thread, which the transcribe/summarize paths catch and
        # clean up after.
        signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGINT, _handler)
```

- **Restore `signal.default_int_handler`, not `SIG_DFL`.** `SIG_DFL` terminates the process immediately — no rollback, no `finally`, and you land straight back in commit A's bug. `default_int_handler` raises `KeyboardInterrupt`, which the code can handle.
- **Never prompt inside the handler.** Set the flag, return, unwind the loop normally, finish transcription, then prompt from ordinary control flow. Interactive I/O in a handler is how you get a wedged terminal and a half-written WAV.
- The recording loop polls `_stop_requested` each tick and exits cleanly. Keep the existing `except KeyboardInterrupt` as a belt-and-braces path.
- Reset `_stop_requested = False` and reinstall the handler at the start of each recording.
- Everything after the loop runs under the default disposition. A Ctrl+C there raises `KeyboardInterrupt` and is handled by the phase it lands in:
  - **during transcription** → rollback per commit A, exit non-zero, **no summarise prompt**
  - **at the prompt** → treat as `n`, exit 0
  - **during summarisation** → abort summarisation only; transcript intact, partial map output preserved, cost line printed for what was spent, exit 0

### Resolution order

```
--summarize with a provider configured   → summarise, no prompt
--summarize with no provider configured  → ERROR, exit non-zero, name the env var
--no-summarize                           → skip, no prompt, always
summarize.auto == "always"               → summarise, no prompt
summarize.auto == "never"                → skip, no prompt
summarize.auto == "ask" (default)        → prompt, Enter = Yes
no provider configured, no flag          → no prompt at all, no message
not interactive (_is_interactive False)  → never prompt; "ask" behaves as "never"
prompt unanswered for prompt_timeout_s   → skip, print the `rec summarize <id>` hint
```

`--summarize` / `--no-summarize` go on **both `rec start` and `rec stop`**. The SIGINT path is `start`'s foreground mode, which the code calls the default UX — so the prompt fires most often from `start`. Flags on `stop` alone can't control it.

Three rules need saying out loud:

**No provider configured → no prompt.** A fresh install behaves exactly as it does today. Prompting someone with no key, so they can press Y and get an error, is worse than not asking. This is what keeps "offline by default" true.

**An explicit flag is explicit intent.** `--summarize` with no provider errors rather than silently skipping. The no-provider rule governs the *implicit* path only.

**Timeout skips rather than proceeds.** Enter means yes; silence does not. Spending the user's API credit because they walked away is a different thing from spending it because they hit Enter.

### Network consent stays separate

The one-time `This sends transcript text to api.z.ai. Continue?` confirmation fires the *first* time a network provider is used. It is not replaced by the `[Y/n]` prompt and must not be folded into it. A habitual Enter on a familiar prompt is not informed consent for a first-ever upload of meeting content. After `confirmed_network: true`, only the `[Y/n]` prompt appears.

---

## §10 — Why the summary never enters `transcript.md`

`summary.md` is the only place the summary lives. This is load-bearing, and the reasoning should survive the next person who thinks inlining would be convenient:

1. **`index.py` indexes `transcript.md`.** Model output in that file means `search_transcripts` returns hits on text the model wrote, and the agent cites it as something said on the call. Manufactured quotes inside a meeting record is the worst failure this product can have.
2. **`rec transcribe <id> --model medium` rewrites `transcript.md` wholesale.** A quality re-run would silently delete a summary the user paid for.
3. **Index churn** on every injection.

Two guard tests keep this true:

- `transcript.md` is byte-identical before and after a successful summarise run. **This is the single most valuable test in the suite** — the invariant it pins is catastrophic and silent when violated.
- The indexer reads only `TRANSCRIPT_FILENAME` and never `summary.md`.

---

## Test seams — build these into commit B

**Decision: injectable seams, not subprocess signal tests.** Subprocess tests are slow and flaky and this project's constraint is near-zero maintenance burden. Real signal delivery gets a line in the manual checklist instead.

1. **`prompt_yes_no(question: str, *, default: bool, timeout_s: float) -> bool`** — the single function that touches stdin. Every prompt test monkeypatches this. No `click.confirm` scattered through `cli.py`.
2. **`_is_interactive() -> bool`** wrapping `sys.stdin.isatty()`. Under CliRunner `isatty()` is always False, which makes a non-TTY test pass vacuously and makes interactive tests impossible. Monkeypatch to `True` to exercise the prompt branch, `False` for the skip branch. Both directions get a test.
3. `prompt_timeout_s` as a config field (default 60). Tests set `0.01`. A literal 60s wait is untestable.

SIGINT-phase tests don't deliver signals — they monkeypatch the phase to raise `KeyboardInterrupt`. That tests the handling, which is where the bugs are.

---

## Test list (all offline, using the existing `conftest.py` XDG redirect and `isolate_logging` fixtures)

**Commit A**
1. `transcriber.transcribe` raises `KeyboardInterrupt` → status rolled back to `RECORDED`, exit non-zero.

**Commit B**
2. Fake provider registered through the registry; every summarize test runs against it. Zero network.
3. `mcp_server` never imports `summarize` or `providers` (extend the read-only guard).
4. `get_summary` returns the file when present, a clear message when absent, creates nothing.
5. `has_summary` is `True`/`False` correctly in both `list_sessions` and `get_session`.
6. Chunker: 3-hour transcript; both line formats; overlap correctness; ceiling never exceeded; concatenating chunks minus overlap reproduces every original line.
7. Tier 3 payload contains no raw chunk text.
8. Cost math against a known token count; unknown model yields `None`, not `0.0`.
9. Cost line matches the documented format.
10. `session.json` contains no key material (assert no substring of any env var value).
11. `--dry-run` makes zero provider calls (assert on the fake provider's call count).
12. Every built-in template loads by name and contains both placeholders.
13. `transcript.md` byte-identical before and after a successful summarise run.
14. Indexer reads only `TRANSCRIPT_FILENAME`.
15. Provider 401 → one human line, no traceback, exit non-zero.

**Commit C**
16. `--no-summarize` never prompts and never calls a provider (on both `start` and `stop`).
17. `--summarize` with no provider → error, exit non-zero, env var named.
18. No provider configured, no flag → no prompt in output at all.
19. `_is_interactive` False → no prompt; `"ask"` skips, `"always"` still runs.
20. `_is_interactive` True, answer `n` → exit 0, transcript intact, zero provider calls, `summary.md` absent.
21. Prompt timeout → skip, hint printed, zero provider calls.
22. Silent session (`STATUS_SILENT`) → no prompt, zero provider calls.
23. Fake provider raises `KeyboardInterrupt` on call 3 → exit 0, transcript byte-identical, cost line printed, partial output preserved.
24. `transcriber.transcribe` raises `KeyboardInterrupt` → exit non-zero, no summarise prompt shown, status `RECORDED`.

---

## Manual acceptance (a clean machine, by hand)

1. No config, `rec summarize <id>` → one-line error naming the env var and config field. Exit non-zero. No traceback, no network call.
2. `export ZAI_API_KEY=…`, provider `glm`, `rec summarize <id> --dry-run` → chunk count, estimated tokens, estimated cost. Zero network.
3. Same session without `--dry-run` on a real 1-hour transcript → `summary.md` written, cost line under $0.01, `session.json` carries the summary block, no key anywhere in the session dir or logs.
4. `rec diagnose <id>` on that session leaks nothing.
5. In Claude Code: `list_sessions` shows `has_summary`, `get_summary` returns it, and the tool cannot generate one.
6. Kill the network mid-run → partial map output preserved, cost line for what was spent, transcript untouched.
7. Real Ctrl+C during a foreground `rec start` → stops, transcribes, prompts.
8. Second Ctrl+C during transcription → exits non-zero, `rec status` shows nothing stuck, `rec list` shows the session as `recorded`.
9. `rec stop` with stdout piped → no prompt, no hang.
10. `pytest` passes with no network, no audio device, no model download.

---

## README and decision log

The README currently states a non-goal: *"Not a meeting summarizer (just the transcript)."* A default-Yes prompt reverses it. That is a decision, not a nicety — log it in `PROJECT_KNOWLEDGE.md` with the date, and replace the line rather than editing around it:

> **Summarisation is opt-in and BYOK.** With no provider configured, `rec` never summarises, never prompts, and never makes a network call — the default install is transcript-only. Configure a key and `rec` will offer to summarise each recording when it finishes. The summary goes to `summary.md`; `transcript.md` stays a verbatim record and nothing is ever written into it.

Add a GLM quickstart: export `ZAI_API_KEY`, set `provider: "glm"`, run `rec summarize <id> --dry-run`, then run it for real.

The other three non-goals stay exactly as written. They're a trust asset and they're still true.

---

## Non-goals for this step — do not build

- No streaming, no async, no concurrency across chunks. Sequential keeps the cost line trivially correct.
- No summary search, no summary indexing, no vector store.
- No summarisation from the MCP server.
- No `rec config` subcommand or interactive setup wizard. A clear error with the exact export line is enough.
- No summarisation on a fresh install, under any circumstances.
- No cross-session or "summarise my week" rollups.
