# Homebrew formula for Call Copilot.
#
# This file lives in a SEPARATE GitHub repo named `homebrew-tap`
# (https://github.com/AnisurRahmann/homebrew-tap) at the path
# `Formula/call-copilot.rb`. Users install with:
#
#   brew install AnisurRahmann/tap/call-copilot
#
# It installs from the published PyPI wheel by name, so pip resolves the FULL
# dependency tree (click, audiotap, faster-whisper, rich, numpy, ...) from
# PyPI automatically — no manual resource stanzas per dependency.
#
# NOTE on the first ~24 hours after a release: Homebrew's `virtualenv_create`
# adds `--uploaded-prior-to=P1D` to its pip calls, a supply-chain safety guard
# that refuses packages published less than ~24h ago. So a brand-new release
# (uploaded today) will fail `brew install` with
# "No matching distribution found for call-copilot" until ~24h have passed.
# This is intentional Homebrew behaviour, not a bug in this formula. Until then,
# `pipx install call-copilot` works immediately. After 24h, `brew install`
# works normally.
#
# Updating for a new release (after `git tag vX.Y.Z` + the release workflow
# publishes to PyPI):
#   1. Look up the new version: https://pypi.org/project/call-copilot/#files
#   2. Update `version` and the `url`/`sha256` below (use the sdist .tar.gz).
#   3. Commit + push to the `homebrew-tap` repo. `brew upgrade` picks it up.

class CallCopilot < Formula
  include Language::Python::Virtualenv

  desc "Silently record meeting audio, then transcribe locally to clean markdown"
  homepage "https://github.com/AnisurRahmann/call-copilot"
  # The sdist is used only as the formula's downloadable + hash-verified source
  # (so Homebrew can version + audit it). The actual install is by name (below),
  # which lets pip resolve the complete dependency tree from PyPI.
  url "https://files.pythonhosted.org/packages/37/7c/e0279aeb30848b1a1d8e597057b1e3a5c0d9756df75df0fedd97fbed7485/call_copilot-0.2.0.tar.gz"
  sha256 "1ac19cc343bde708b60c19334ff5b223a7c975853bffe7e17843d110f8419c52"
  license "MIT"
  head "https://github.com/AnisurRahmann/call-copilot.git", branch: "main"

  # audiotap's native extension is built per macOS arch, so this formula is
  # macOS-only (which is also the only place Core Audio taps exist).
  depends_on :macos

  # Homebrew's python@3.12 provides the interpreter. We do NOT bundle
  # setuptools/wheel ourselves — faster-whisper / numpy ship macOS wheels
  # (arm64 + x86_64), so nothing needs to compile from source.
  depends_on "python@3.12"

  def install
    # Create an isolated venv under libexec and install call-copilot BY NAME.
    # This is the key choice: `pip_install_and_link "call-copilot==#{version}"`
    # tells pip to resolve the package AND its entire dependency tree from PyPI
    # (click, audiotap, faster-whisper, rich, numpy, pydantic, soundfile...),
    # rather than enumerating each dep as a separate Homebrew `resource`.
    # `pip_install_and_link` also links the `rec` console script onto PATH.
    venv = virtualenv_create(libexec, "python3.12")
    venv.pip_install_and_link "call-copilot==#{version}"
  end

  test do
    # --version bypasses the macOS-version gate, so it works in Homebrew CI
    # without an audio device or macOS 14.2 (the runner's macOS is supported,
    # but this guards against any environment). Matches the version we built.
    assert_match version.to_s, shell_output("#{bin}/rec --version")
  end
end
