# Homebrew formula for Call Copilot.
#
# This file lives in a SEPARATE GitHub repo named `homebrew-tap`
# (https://github.com/AnisurRahmann/homebrew-tap) at the path
# `Formula/call-copilot.rb`. Users install with:
#
#   brew install AnisurRahmann/tap/call-copilot
#
# HOW THIS FORMULA INSTALLS (important — read before editing):
# It does NOT use Homebrew's `Language::Python::Virtualenv` helpers
# (`virtualenv_create` / `venv.pip_install`), because those inject two flags
# that break this package:
#   - `--no-deps`       -> would skip call-copilot's dependency tree
#                          (click, audiotap, faster-whisper, ...), and `rec`
#                          would crash with "No module named 'click'".
#   - `--uploaded-prior-to=P1D` -> refuses any PyPI file published <24h ago,
#                          so a fresh release fails with
#                          "No matching distribution found".
# Instead we create the venv with plain `python -m venv` and call `pip install`
# directly, which resolves the full dependency tree from PyPI and is not subject
# to the freshness guard. `brew install` therefore works immediately on release.
#
# Updating for a new release (after `git tag vX.Y.Z` + the release workflow
# publishes to PyPI):
#   1. Look up the new version: https://pypi.org/project/call-copilot/#files
#   2. Update the `url`/`sha256` below (use the sdist .tar.gz).
#   3. Commit + push to the `homebrew-tap` repo. `brew upgrade` picks it up.

class CallCopilot < Formula
  desc "Silently record meeting audio, then transcribe locally to clean markdown"
  homepage "https://github.com/AnisurRahmann/call-copilot"
  # The sdist is the formula's hash-verified source (so Homebrew can version +
  # audit it). The actual install fetches the wheel from PyPI by name.
  url "https://files.pythonhosted.org/packages/aa/30/e4218e84b31e87e12d76e2b7353f6a5889bc17e72b86f1542e25ba048632/call_copilot-0.3.0.tar.gz"
  sha256 "d9f71f4ce4efe8072e1214bfd5877651610975de636d870549776293191b9500"
  license "MIT"
  head "https://github.com/AnisurRahmann/call-copilot.git", branch: "main"

  # audiotap's native extension is macOS-only, as is the Core Audio API it
  # wraps. No point offering this on Linux.
  depends_on :macos

  # Homebrew's python@3.12 provides the interpreter.
  depends_on "python@3.12"

  def install
    # Create an isolated venv under libexec, bootstrap pip into it, then install
    # call-copilot by name+version. Calling pip directly (rather than via
    # Homebrew's venv.pip_install wrapper) means pip resolves the FULL
    # dependency tree from PyPI and isn't subject to Homebrew's --no-deps /
    # --uploaded-prior-to=P1D flags. Verified: resolves 35 packages.
    venv_bin = libexec/"bin"
    system Formula["python@3.12"].opt_bin/"python3.12", "-m", "venv", libexec
    system venv_bin/"python", "-m", "pip", "install", "--upgrade", "pip"
    system venv_bin/"python", "-m", "pip", "install", "call-copilot==#{version}"

    # Link only the user-facing CLI onto PATH (deps stay isolated in libexec).
    bin.install_symlink venv_bin/"rec"
  end

  def caveats
    <<~EOS
      call-copilot is ready to use. Run:

          rec setup     # one-time: verify macOS, audio taps, and permissions
          rec start     # start recording — press Ctrl+C to stop & transcribe

      Heads up on the install log:
      You may have seen "Failed to fix install linkage" for
      audiotap/libaudiotap.dylib. It is expected and harmless — Homebrew tries
      to rewrite the dylib's id, but its mach-o header was built without spare
      space, so the rewrite is skipped. The library is loaded by absolute path
      via ctypes and `rec` works correctly. No action needed.
    EOS
  end

  test do
    # --version bypasses the macOS-version gate, so it works in Homebrew CI
    # without an audio device. Matches the version we built.
    assert_match version.to_s, shell_output("#{bin}/rec --version")
  end
end
