"""Call Copilot — a terminal meeting recorder (command: `rec`).

Records system audio in the background via macOS Core Audio taps (the
`audiotap` library — zero degradation of what you hear, no virtual audio
driver) and transcribes locally with faster-whisper into clean markdown.
"""

__version__ = "0.3.0"
