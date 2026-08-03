# Contributing to Call Copilot

Thanks for your interest in Call Copilot! This project is small and focused — a
terminal meeting recorder for macOS that transcribes locally. The easiest way to
help is one of these:

1. **File an issue** for a bug, a missing feature, or a doc gap.
2. **Open a pull request** for a concrete fix. Small, focused PRs land fastest.

## Project scope

Call Copilot deliberately does one thing: record meeting audio and produce a clean
local transcript. It is **not** aiming to become a meeting summarizer, a calendar
integrator, or a cloud product. If your change broadens the scope, open an issue to
discuss it before investing in a PR — it may be a better fit as a separate tool.

## Development setup

You need **macOS 14.2+** (the `audiotap` dependency uses Core Audio process taps,
which is macOS-only) and **Python 3.11+**.

```bash
git clone https://github.com/AnisurRahmann/call-copilot.git
cd call-copilot
make install          # creates .venv and installs the package + dev deps (editable)
```

## Running the tests

```bash
make test             # == .venv/bin/python -m pytest tests -v
```

The suite is **fully offline**: no audio device, no microphone permission, and no
Whisper model download is required. It mocks `audiotap`, `faster-whisper`, and the
filesystem, so it runs on any macOS box (including CI) without setup. Expect **89
passing** on a clean tree.

> The tests pass anywhere, but running the actual `rec` commands still requires
> macOS 14.2+ with capture permissions granted.

## Code style

- **Lint:** [ruff](https://docs.astral.sh/ruff/) is configured in `pyproject.toml`
  (`target-version = "py311"`, `line-length = 100`). Run `ruff check .` before
  pushing; CI enforces it.
- **Python:** 3.11+. New code may use modern syntax (`X | None`, `match`, etc.).
- Match the surrounding style — module docstrings, `from __future__ import
  annotations`, type hints, and `log.debug/info/warning` via the `log.py` helper.

## Pull requests

1. **Branch** off `main`. Use a descriptive name: `feat/...`, `fix/...`, `docs/...`.
2. **Keep it focused.** One logical change per PR; put unrelated work in separate PRs.
3. **Add or update tests** for any behavior change. The test suite is the contract.
4. **Run the gate locally** before pushing:
   ```bash
   make test
   .venv/bin/ruff check .
   ```
5. **Reference the issue** your PR addresses in the description (e.g. `Closes #12`).
6. Be patient and kind. This is a small project; review may take a few days.

If a change touches audio capture, transcription output, or the session file format,
call that out explicitly in the PR description — those are the load-bearing parts.

## Reporting bugs

Open an [issue](https://github.com/AnisurRahmann/call-copilot/issues) and include:

- macOS version and chip (Intel / Apple Silicon).
- Python version (`python3 --version`).
- The exact `rec` command(s) you ran and the flags used.
- The output of `rec diagnose <session-id> --stdout` (it bundles logs + metadata
  without including the audio itself).

## Security issues

Do **not** open a public issue for security problems. See [SECURITY.md](SECURITY.md)
for private reporting.

## Code of conduct

By participating you agree to uphold the [Code of Conduct](CODE_OF_CONDUCT.md).
