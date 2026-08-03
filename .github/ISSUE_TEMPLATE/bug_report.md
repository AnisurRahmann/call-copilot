---
name: Bug report
about: Something isn't working as expected
title: "[bug] "
labels: bug
---

## What happened

<!-- A clear description of what went wrong, including any error output. -->

## What I expected

<!-- What you thought would happen instead. -->

## How to reproduce

```bash
# The exact rec commands you ran, in order.
rec setup
rec start ...
```

## Environment

- **macOS version:** <!-- e.g. 14.5 -->
- **Chip:** <!-- Apple Silicon / Intel -->
- **Python version:** <!-- `python3 --version` -->
- **How you installed `rec`:** <!-- pip / pipx / `make install` / editable -->
- **Flags / options used:** <!-- e.g. --mic-only, --model medium -->

## Debug bundle

If you can, attach or paste the output of:

```bash
rec diagnose <session-id> --stdout
```

(It bundles session metadata and logs — **not** the audio itself. Check the
output before sharing if your logs may reference sensitive content.)
