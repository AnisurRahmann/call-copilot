# Homebrew tap for Call Copilot

`rec` is distributed on Homebrew via a **separate** GitHub repository that acts
as a tap. Users install with one command:

```bash
brew install AnisurRahmann/tap/call-copilot
```

This document explains how that tap works and how to set it up / keep it current.
The formula itself is mirrored in this repo at
[`Formula/call-copilot.rb`](../Formula/call-copilot.rb) as the source of truth;
the tap repo is just where Homebrew looks for it.

## Why a separate repo?

Homebrew resolves `brew install <user>/tap/<name>` by cloning
`https://github.com/<user>/homebrew-tap` and looking for
`Formula/<name>.rb` inside it. The tap repo is intentionally tiny — just the
formula — so the `call-copilot` source repo stays focused on the app. There's
no build step on the Homebrew side: the formula tells Homebrew to `pip install`
the published PyPI sdist into an isolated virtualenv.

## One-time: create the tap repo

1. Create an **empty** public GitHub repo named **`homebrew-tap`** under your
   account (`AnisurRahmann/homebrew-tap`). The name must be exactly
   `homebrew-tap` — that's how `brew tap AnisurRahmann/tap` finds it.

2. Copy the formula into it at the path `Formula/call-copilot.rb`:

   ```
   homebrew-tap/
   └── Formula/
       └── call-copilot.rb
   ```

   ```bash
   git clone https://github.com/AnisurRahmann/homebrew-tap.git
   mkdir -p homebrew-tap/Formula
   cp Formula/call-copilot.rb homebrew-tap/Formula/call-copilot.rb
   cd homebrew-tap
   git add Formula/call-copilot.rb
   git commit -m "Add call-copilot 0.1.0"
   git push
   ```

3. Verify it's discoverable:

   ```bash
   brew tap AnisurRahmann/tap
   brew info call-copilot
   ```

## Updating after a release

Every release on PyPI needs the formula bumped. The release workflow
(`.github/workflows/release.yml`) builds + publishes the sdist + wheel to PyPI
on a `v*` tag. After it succeeds:

1. Find the sdist URL + hash on PyPI:
   <https://pypi.org/project/call-copilot/#files>

2. Compute the SHA-256 of the `.tar.gz`:

   ```bash
   curl -sL https://files.pythonhosted.org/packages/.../call_copilot-X.Y.Z.tar.gz | shasum -a 256
   ```

3. In **`homebrew-tap/Formula/call-copilot.rb`**, update the top-level
   `url` (the sdist URL) and `sha256`. Homebrew infers the `version` from the
   URL filename, so keep that consistent.

4. Commit + push to `homebrew-tap`. Existing users get the update on
   `brew upgrade`.

> ⏱️ **The ~24h freshness guard.** Homebrew's `virtualenv_create` adds
> `--uploaded-prior-to=P1D` to its pip calls — a supply-chain safety measure
> that refuses PyPI packages published less than ~24 hours ago. So a brand-new
> release will fail `brew install` with
> `No matching distribution found for call-copilot` until ~24h have passed
> since the PyPI upload. This is intentional Homebrew behaviour, not a bug in
> the formula. Until the guard lapses, `pipx install call-copilot` works
> immediately (pipx has no such guard).

## How the formula works

- **`depends_on :macos`** — `audiotap` (the native Core Audio taps binding) is
  macOS-only, as is the API it wraps. There's no point offering this on Linux.
- **`depends_on "python@3.12"`** — pins a Python the formula controls, so the
  user's system Python version doesn't matter.
- **`virtualenv_create` + `pip_install_and_link "call-copilot==<version>"`** —
  installs the package **by name**, letting pip resolve the full dependency
  tree (audiotap, faster-whisper, rich, click, numpy, …) from PyPI into an
  isolated virtualenv under `libexec`, then links `rec` onto `PATH`. This
  avoids enumerating every transitive dep as a separate Homebrew `resource`.
  The sdist `url`/`sha256` at the top are Homebrew's auditable source record;
  the actual install fetches the wheel from PyPI.
- **`test do`** — runs `rec --version` (which bypasses the macOS-version gate,
  so it passes in Homebrew's CI without an audio device) and checks the version
  string matches.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No matching distribution found for call-copilot` | The release is <24h old — Homebrew's freshness guard. Wait ~24h after the PyPI upload, or use `pipx install call-copilot` in the meantime. |
| `brew install` says checksum mismatch | The `sha256` in the formula doesn't match the sdist on PyPI. Recompute it (see above) and push to `homebrew-tap`. |
| `Error: call-copilot: no bottle` | Not an error — this is a source build from PyPI. The first `brew install` pulls prebuilt macOS wheels for `faster-whisper`/`numpy`; subsequent upgrades are fast. |
| `rec` not found after install | Run `brew link call-copilot` or check `brew --prefix call-copilot/bin`. |
| `rec` runs but warns about macOS version | The machine is below 14.2; see the main README's Requirements. The Homebrew install succeeds but `rec` refuses to run, by design. |
