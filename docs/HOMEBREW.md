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
(`.github/workflows/release.yml`) builds + publishes the sdist to PyPI on a `v*`
tag. After it succeeds:

1. Find the sdist URL + hash on PyPI:
   <https://pypi.org/project/call-copilot/#files>

2. Compute the SHA-256 of the `.tar.gz`:

   ```bash
   curl -L "https://files.pythonhosted.org/packages/source/c/call-copilot/call-copilot-X.Y.Z.tar.gz" | shasum -a 256
   ```

3. In **`homebrew-tap/Formula/call-copilot.rb`**, update both `url`/`sha256`
   pairs (the top-level one **and** the `resource` block) and the `version`
   method (Homebrew infers it from the `url`, but keep them consistent).

4. Commit + push to `homebrew-tap`. Existing users get the update on
   `brew upgrade`; the formula revision is automatic.

> The placeholder `sha256` of all zeros in the formula is **deliberate** —
> Homebrew refuses to install on a checksum mismatch, so the first real release
> must replace it. Don't merge a real `0.1.0` formula until the sdist is on PyPI.

## How the formula works

- **`depends_on :macos`** — `audiotap` (the native Core Audio taps binding) is
  macOS-only, as is the API it wraps. There's no point offering this on Linux.
- **`depends_on "python@3.12"`** — pins a Python the formula controls, so the
  user's system Python version doesn't matter.
- **`virtualenv_create` + `pip_install`** — installs the sdist and all its
  declared dependencies (audiotap, faster-whisper, rich, click, …) into an
  isolated virtualenv under `libexec`, then symlinks only `rec` onto the
  user's `PATH`. No dependency hell with other Homebrew Python tools.
- **`test do`** — runs `rec --version` (which bypasses the macOS-version gate,
  so it passes in Homebrew's CI without an audio device) and checks the version
  string matches.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `brew install` says checksum mismatch | The `sha256` in the formula doesn't match the sdist on PyPI. Recompute it (see above) and push to `homebrew-tap`. |
| `Error: call-copilot: no bottle` | Not an error — this is a source build from PyPI. The first `brew install` compiles `faster-whisper`/`numpy` wheels (a minute or two); upgrades are faster. |
| `rec` not found after install | Run `brew link call-copilot` or check `brew --prefix call-copilot/bin`. |
| `rec` runs but warns about macOS version | The machine is below 14.2; see the main README's Requirements. The Homebrew install succeeds but `rec` refuses to run, by design. |
