"""Local browser UI for Call Copilot (`rec web`).

A loopback-only HTTP server (`server.py`) that serves a single-page app for
viewing sessions, playing session audio, reading transcripts, searching, and
starting/stopping a recording. The frontend is one file (`static/index.html`)
with inline CSS and vanilla JS — no build step, no framework.

This subpackage mutates state (start/stop recording, runs transcription),
which is why it lives here and not in `mcp_server.py` (the strictly read-only
surface). The two never import each other.
"""
