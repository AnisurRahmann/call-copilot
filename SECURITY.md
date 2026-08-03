# Security Policy

## Reporting a vulnerability

**Please do not open a public GitHub issue for security problems.**

If you believe you've found a security vulnerability in Call Copilot, report it
privately so it can be triaged before public disclosure. Use one of:

1. **GitHub Security Advisories** (preferred): go to
   [github.com/AnisurRahmann/call-copilot/security/advisories/new](https://github.com/AnisurRahmann/call-copilot/security/advisories/new)
   and select "Report a vulnerability." (This requires the repository's
   *Private vulnerability reporting* setting to be enabled — see the note below.)
2. **Email**: send details to **shakilwizard@gmail.com** with `[call-copilot
   security]` in the subject line.

Please include, where possible:

- A description of the issue and its potential impact.
- Steps to reproduce (commands, config, OS version).
- Any suggested mitigation or fix.

## What to expect

You should get an acknowledgement within a few days. We'll work with you to
understand the issue and coordinate a fix and disclosure timeline.

## Scope and threat model

Call Copilot runs entirely on your machine: it records audio to your local disk
and transcribes it locally with faster-whisper. The only network access is the
first-run download of a Whisper model from Hugging Face. There is no server
component and no remote API. Vulnerabilities of interest include anything that
exposes recorded audio or transcripts to another local user/process, crashes or
corrupts sessions, or causes unexpected capture/recording behavior.

## Note for the maintainer

The GitHub Security Advisories link above works only once **Private
vulnerability reporting** is enabled in the repo: *Settings → Code security and
analysis → Private vulnerability reporting → Enable*. Turn this on when (or
before) you first push this file to the public repo.
