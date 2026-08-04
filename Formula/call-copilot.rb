# Homebrew formula for Call Copilot.
#
# This file lives in a SEPARATE GitHub repo named `homebrew-tap`
# (https://github.com/AnisurRahmann/homebrew-tap) at the path
# `Formula/call-copilot.rb`. Users install with:
#
#   brew install AnisurRahmann/tap/call-copilot
#   # equivalent to:
#   brew tap AnisurRahmann/tap
#   brew install call-copilot
#
# It installs from the published PyPI sdist (not from git), so it tracks real
# releases. The native `audiotap` wheel is macOS-only and arm64/x86_64, which
# matches Homebrew's macOS-only support — no cross-platform caveats.
#
# Updating for a new release (after `git tag vX.Y.Z` + the release workflow
# publishes to PyPI):
#   1. Look up the sdist: https://pypi.org/project/call-copilot/#files
#   2. `shasum -a 256 <the .tar.gz>` (or `curl -L <url> | shasum -a 256`)
#   3. Update `version` and `sha256` below.
#   4. Commit to the `homebrew-tap` repo on its default branch. `brew upgrade`
#      picks it up automatically.

class CallCopilot < Formula
  include Language::Python::Virtualenv

  desc "Silently record meeting audio, then transcribe locally to clean markdown"
  homepage "https://github.com/AnisurRahmann/call-copilot"
  url "https://files.pythonhosted.org/packages/source/c/call-copilot/call-copilot-0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  # When publishing the first real release, replace the placeholder sha256
  # above with the real value (see header comment). Homebrew refuses to
  # install on a checksum mismatch, so a wrong/zero hash fails loudly.
  license "MIT"
  head "https://github.com/AnisurRahmann/call-copilot.git", branch: "main"

  # audiotap's native extension is built per macOS arch, so this formula is
  # macOS-only (which is also the only place Core Audio taps exist).
  depends_on :macos

  # Homebrew's Python 3.11+ is provided automatically by `python` (uses the
  # brewed python@3.12 as the runtime). faster-whisper / numpy wheels exist
  # for both arm64 and x86_64 on macOS, so no `:build` deps are needed.
  depends_on "python@3.12"

  resource "call-copilot" do
    # The sdist already carries the full dependency set in its metadata, so we
    # let pip resolve transitive deps in one install rather than enumerating
    # every resource by hand.
    url "https://files.pythonhosted.org/packages/source/c/call-copilot/call-copilot-0.1.0.tar.gz"
    sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  end

  def install
    venv = virtualenv_create(libexec, "python3.12")
    venv.pip_install resource("call-copilot")
    # Link only the user-facing CLI onto PATH (keeps deps isolated in libexec).
    bin.install_symlink Dir["#{libexec}/bin/rec"]
  end

  test do
    # --version and --help bypass the macOS-version gate, so they work in CI
    # without an audio device. This is the same command `make test` checks.
    assert_match version.to_s, shell_output("#{bin}/rec --version")
  end
end
