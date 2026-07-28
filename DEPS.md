# DEPS.md — Meeting Recorder Dependency Reference

> Auto-compiled reference for AI coding agent. Contains exact API surfaces,
> code patterns, and gotchas for each dependency. All signatures below were
> verified against primary docs/source (not inferred) by research agents on
> 2026-07-27. Where a signature was corrected against source vs. README,
> it is flagged inline.

**Platform target:** macOS, Apple Silicon (M-series), CPU-only by default.
**Tool purpose:** Python CLI (`rec`) that records system audio via BlackHole,
writes streaming WAV, and transcribes locally with faster-whisper.

---

## Table of contents

1. [BlackHole 2ch](#1-blackhole-2ch)
2. [SwitchAudioSource](#2-switchaudiosource)
3. [python-sounddevice](#3-python-sounddevice)
4. [python-soundfile](#4-python-soundfile)
5. [faster-whisper](#5-faster-whisper)
6. [Rich](#6-rich)
7. [Click](#7-click)

---

## 1. BlackHole 2ch

### How it works

BlackHole is a virtual audio loopback driver for macOS. It allows applications
to pass audio to other applications: the sending application sets its output to
BlackHole, and the receiving application sets its input to BlackHole. Core Audio
natively uses 32-bit float audio at the system level. The driver runs natively
on the system audio framework with "no kernel extensions or modifications to
system security necessary." BlackHole 2ch is the standard pre-built variant
(also shipped as 16ch, 64ch). Native Apple Silicon support was added in v0.2.8
(older builds required Rosetta).

Multi-Output Device (macOS concept, not BlackHole-specific) is a Core Audio
construct that fans one audio stream out to multiple physical/virtual endpoints
simultaneously. It is the mechanism used to record system audio through
BlackHole while still hearing playback through real speakers/headphones.

**Drift correction** is required because each device has its own clock. Enable
drift correction for every device in the Multi-Output Device **EXCEPT** the
Clock Source (also called the Master Device or Primary Device). The clock
source is the device whose sample clock all others are resampled against;
without drift correction on the others, audio drifts out of sync over time.

**Device ordering matters:** due to a macOS bug, a standard 2-channel device
(the Built-in Output, or another 2ch device, or BlackHole 2ch) must be enabled
and listed as the TOP device — i.e. the primary/clock device. If BlackHole ends
up first and is multi-channel, uncheck and re-check its box to change the
ordering. Built-in Output must be enabled and listed as the top device.

### Device names and conventions

- Pre-built BlackHole variant names exactly as they appear in the system audio
  device list: `BlackHole 2ch`, `BlackHole 16ch`, `BlackHole 64ch`.
- The underlying driver bundle file is named `BlackHoleXch.driver` where X is
  2, 16, or 64 (e.g. `BlackHole2ch.driver`).
- macOS assigns the default name `Multi-Output Device` to a newly created
  multi-output aggregate (created via Audio MIDI Setup). This is the literal
  string a CLI tool should expect/match when scanning for it.
- For custom builds, the name is controlled by the pre-compiler constants
  `kDriver_Name`, `kDevice_Name`, and `kPlugIn_BundleID`. Channel count is set
  via `kNumber_Of_Channels` (custom builds commonly use 2, 16, 64, 128, 256).

### Multi-Output Device setup steps

The literal numbered procedure (print these verbatim to the CLI user):

1. Open Audio MIDI Setup: press Command+Space (Spotlight) and type
   `Audio MIDI Setup`. Alternatively, open Applications > Audio MIDI Setup.
2. If the device list is not visible, select "Audio Devices" from the Windows
   drop-down menu.
3. Click the `+` button in the lower-left corner and select
   "Create Multi-Output Device". A new device named `Multi-Output Device`
   appears in the sidebar.
4. Check the boxes for the output devices you want: your
   speakers/headphones/USB interface **AND** `BlackHole 2ch`.
5. Make sure the Built-in Output (or another standard 2-channel device) is
   enabled and listed as the TOP device in the list — this is the primary
   device / clock source. If BlackHole is listed first, uncheck and re-check
   its box to reorder.
6. Set the Built-in Output (your speakers/headphones) as the Clock Source /
   Master Device.
7. Enable **Drift Correction** for ALL devices EXCEPT the Clock Source (so
   enable it for BlackHole 2ch and any other non-master devices). This
   prevents out-of-sync and crackling audio.
8. Ensure both devices use the SAME sample rate (typically 44.1 kHz or
   48 kHz). Mismatched sample rates cause crackling/sync issues.
9. Right-click the Multi-Output Device and select "Use This Device For Sound
   Output" (or select it under System Settings > Sound).
10. In your recording application, choose `BlackHole 2ch` as the audio
    input/source. Verify the recording software is listening to BlackHole,
    not the speakers.

For combining a microphone with system audio, use a separate Aggregate Device
(different from Multi-Output Device).

### Known issues and gotchas

- **Volume control:** macOS does NOT support changing the volume of a
  Multi-Output Device. The keyboard volume keys and system volume slider are
  disabled/grayed out when the Multi-Output Device is the active output. Volume
  can only be adjusted per-device inside Audio MIDI Setup. (Workaround: control
  volume on individual hardware devices, or use a third-party tool.)
- **System sounds / startup chime:** Because the Multi-Output Device does not
  support the standard volume-control path used by a single device, system
  alert sounds and the startup chime may not play, or play at a fixed/default
  level. The startup chime is firmware-level and routes only through the
  primary Built-in Output, not a Multi-Output Device.
- **Buggy apps:** The BlackHole README explicitly lists apps known to be buggy
  or non-functional with Multi-Output Devices: **Apple Podcasts, Apple
  Messages, and HDHomeRun**.
- **iOS apps on Apple Silicon Macs (M1, M1 Pro, M2, etc.):** iOS/iPad apps
  running natively on Apple Silicon CANNOT output sound to a Multi-Output
  Device (or Aggregate Device). BlackHole can only capture sound from native
  macOS apps, not from iOS apps running under the iOS-on-macOS subsystem.
  Apple's iOS apps refuse to recognize aggregate audio devices. Maintainer
  workaround: do NOT use a Multi-Output Device for these apps — instead set
  the system output directly to BlackHole, then route BlackHole's input to
  your speakers via a DAW with input monitoring, or set OBS desktop-audio
  source to BlackHole and OBS audio monitoring to speakers/headphones.
  Maintainer calls this "needlessly complicated but that's the solution."
- **DRM-protected audio:** DRM content (e.g. some streaming/music apps) is
  intentionally muted or blocked from capture by virtual audio drivers. Not
  documented on the BlackHole wiki itself — flagged as a macOS/platform
  behavior.
- **AirPods gotcha:** AirPod microphones operate at a lower sample rate and
  therefore CANNOT serve as the primary/clock device in an aggregate or
  Multi-Output setup. The fix: use the Built-in Output (built-in speakers), or
  BlackHole 2ch, as the primary/clock device instead of AirPods.
- **High channel + high sample rate warning:** Do not use high sample rates
  together with a high number of channels — the system may fail to process the
  audio stream efficiently.
- **Installer failure:** macOS sometimes fails to install the .pkg depending
  on file location. Workaround: move the .pkg from Downloads to Desktop (or
  vice-versa) and re-run.
- **Silent recording troubleshooting:** If the recording is silent, verify
  (a) the Mac's sound output is set to the Multi-Output Device, and (b) the
  recording software is listening to BlackHole, not the speakers. If only
  BlackHole is enabled with no listening hardware, audio is captured but
  inaudible.

### Sample rate and channel constraints (reference)

- Supported BlackHole sample rates: 8, 16, 44.1, 48, 88.2, 96, 176.4, 192,
  352.8, 384, 705.6, 768 kHz.
- In a Multi-Output Device, ALL member devices must share the SAME sample
  rate; 44.1 kHz or 48 kHz is the practical default. Mismatch causes
  crackling/desync.
- Core Audio bit depth at system level: 32-bit float.
- Pre-built channel counts: 2ch, 16ch, 64ch (BlackHole 2ch is the recommended
  variant for Multi-Output / recording use).

---

## 2. SwitchAudioSource

`switchaudio-osx` (deweller/switchaudio-osx, MIT) is a macOS-only CoreAudio
CLI. The binary is named **`SwitchAudioSource`** (that is the Xcode scheme name
and what Homebrew installs). The project was historically distributed under the
names `AudioSwitch` / `AudioSwitchCmd` in older revisions, but the current
source and README use `SwitchAudioSource` exclusively — use that name in
subprocess calls.

### CLI reference

The flag parser is built from the getopt string `"hacm:nt:f:i:u:s:"` (verified
in `audio_switch.c`). Flags that take an argument: `-m`, `-t`, `-f`, `-i`,
`-u`, `-s`. Flags without an argument: `-h`, `-a`, `-c`, `-n`.

| Short | Long meaning | Takes arg? | Description (verbatim from `showUsage()`) |
|-------|-------------|-----------|--------------------------------------------|
| `-a` | show all | no | Shows all devices. |
| `-c` | show current | no | Shows current device. If `-t` is omitted, type defaults to **output**. |
| `-f` | format | yes (`cli`/`human`/`json`) | Output format. Defaults to `human`. |
| `-t` | type | yes (`input`/`output`/`system`/`all`) | Device type. Defaults to `output`. **Note:** the README usage line lists only `input/output/system`, but the source also accepts `all`. |
| `-m` | mute | yes (`mute`/`unmute`/`toggle`) | Sets the mute status (mute/unmute/toggle). For input/output only (per source usage text). |
| `-n` | cycle next | no | Cycles the audio device to the next one. |
| `-i` | set by id | yes (integer) | Sets the audio device by numeric CoreAudio device id. Value is parsed with `atoi`. |
| `-u` | set by uid | yes (string) | Sets the audio device by uid **or a substring of the uid** (uses `strstr`, i.e. substring match). |
| `-s` | set by name | yes (string) | Sets the audio device by **exact** name (uses `strcmp` — must match the full device name verbatim). |
| `-h` | help | no | **Undocumented in README but accepted.** Shows usage and exits 0. |

The README's printed usage line is
`SwitchAudioSource [-a] [-c] [-f format] [-t type] [-n] -s device_name | -i device_id | -u device_uid`
(note the README omits `-m` from the usage line, though `-m` is fully
supported).

The flags `-a`, `-c`, `-h`, `-n`, and the set actions `-i`/`-u`/`-s`/`-m` all
write into a single `function` variable inside the option loop, so the
**last-occurring action flag wins** if more than one is supplied.

### Output formats (json examples)

`-f` accepts exactly three values (`cli`, `human`, `json`); anything else
prints `Unknown format <x>` + usage and exits 1.

- **`human`** (default): device names only, one per line.
- **`cli`**: CSV-style, fields `name,type,id,uid`.
- **`json`**: **JSON Lines / NDJSON** — one JSON object per line, **not** a
  JSON array. The exact printf format string from source is:
  `{"name": "%s", "type": "%s", "id": "%u", "uid": "%s"}`

Critical detail: because the source does
`printf("{\"name\": \"%s\", \"type\": \"%s\", \"id\": \"%u\", \"uid\": \"%s\"}\n", ...)`,
the `id` field is rendered as a **string** (the `%u` is placed inside literal
quotes), e.g. `"id": "73"`. There is no `"current"`/`"default"` field in the
JSON — the current/selected device is not flagged in `-a` output. The keys are
exactly `name`, `type`, `id`, `uid`.

Sample output of `SwitchAudioSource -a -f json` (verbatim schema; device values
illustrative):

```json
{"name": "MacBook Pro Speakers", "type": "output", "id": "73", "uid": "BuiltInSpeakerDevice"}
{"name": "BlackHole 2ch", "type": "output", "id": "74", "uid": "BlackHole2ch_UID"}
{"name": "MacBook Pro Microphone", "type": "input", "id": "75", "uid": "BuiltInMicrophoneDevice"}
```

`-a -t all -f json` emits the same one-object-per-line format but lists inputs
first, then outputs. `SwitchAudioSource -c -f json` emits a **single** object
line for the current device:

```json
{"name": "MacBook Pro Speakers", "type": "output", "id": "73", "uid": "BuiltInSpeakerDevice"}
```

### Code integration patterns (subprocess calls from Python)

How-to recipes (all verified against source behavior):

- List all devices: `SwitchAudioSource -a` (defaults to type `output`); use
  `-t all` for both input+output, `-t input`, or `-t output`.
- Get currently selected **output** device: `SwitchAudioSource -c` (type
  defaults to output).
- Get currently selected **input** device: `SwitchAudioSource -c -t input`.
- Switch **output** device by name: `SwitchAudioSource -s "BlackHole 2ch"`
  (type defaults to output).
- Switch **input** device by name:
  `SwitchAudioSource -s "MacBook Pro Microphone" -t input`.
- Mute/unmute/toggle: `SwitchAudioSource -m toggle -t input` (README's headline
  example), `-m mute`, or `-m unmute`. Restrict with `-t input` or `-t output`;
  `-t all` mutes both. Muting `system` is rejected.

Copy-pasteable commands:

```bash
# List all devices (human readable)
SwitchAudioSource -a -t all

# List all devices (parseable JSON Lines)
SwitchAudioSource -a -t all -f json

# Get current OUTPUT device name
SwitchAudioSource -c

# Get current INPUT device name
SwitchAudioSource -c -t input

# Switch output device to "BlackHole 2ch"
SwitchAudioSource -s "BlackHole 2ch"

# Switch input device to "MacBook Pro Microphone"
SwitchAudioSource -s "MacBook Pro Microphone" -t input

# Toggle mute on the currently selected INPUT (e.g. microphone)
SwitchAudioSource -m toggle -t input
# Explicit mute / unmute of the currently selected OUTPUT
SwitchAudioSource -m mute   -t output
SwitchAudioSource -m unmute -t output
```

Python subprocess pattern. Note three things the source forces on you:
(1) JSON output is JSON Lines, so parse line by line rather than
`json.loads()`-ing the whole blob; (2) error messages are written to **stdout**
via `printf`, not stderr, so you must parse `stdout` even on failure; (3) a
successful "device not found" returns exit code 1, but a CoreAudio
`AudioObjectSetPropertyData` failure inside `setOneDevice` still returns 0 —
so do not rely on a 0 exit to mean "the switch actually took effect".

```python
import json
import subprocess

SWITCH_BIN = "SwitchAudioSource"

def _run(args: list[str]) -> subprocess.CompletedProcess:
    # capture_output=True grabs both streams; text=True decodes as UTF-8.
    # Do NOT use check=True: nonzero exits are expected (e.g. unknown device).
    return subprocess.run(
        [SWITCH_BIN, *args],
        capture_output=True,
        text=True,
        check=False,
    )

def list_devices(dev_type: str = "all") -> list[dict]:
    """Return all devices of the given type as parsed JSON objects.

    switchaudio emits JSON Lines (one object per line), NOT a JSON array.
    """
    proc = _run(["-a", "-t", dev_type, "-f", "json"])
    # Exit 1 here would mean a parse/format error, not 'no devices';
    # empty stdout simply means no devices of that type.
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

def get_current_device_name(dev_type: str = "output") -> str:
    """Human format prints just the device name with no quoting."""
    proc = _run(["-c", "-t", dev_type])
    return proc.stdout.strip()

def set_device_by_name(name: str, dev_type: str = "output") -> None:
    """Switch device. Raises if the device name does not exist."""
    proc = _run(["-s", name, "-t", dev_type])
    if proc.returncode != 0:
        # stdout (not stderr) holds:
        # Could not find an audio device named "X" of type output.  Nothing was changed.
        raise RuntimeError(f"switch failed ({proc.returncode}): {proc.stdout.strip()}")

# --- Restore pattern: capture the original device, switch, then switch back ---
def with_temporary_output_device(target: str):
    original = get_current_device_name("output")  # save first
    try:
        set_device_by_name(target, "output")
        yield original
    finally:
        # Best-effort restore; ignore the missing-device case on cleanup.
        _run(["-s", original, "-t", "output"])
```

### Gotchas

- **JSON is JSON Lines, not an array.** `SwitchAudioSource -a -f json` prints
  one `{"name","type","id","uid"}` object per line. `json.loads()` on the full
  output raises; iterate `splitlines()`.
- **`id` is a JSON string, not a number.** Source uses `"id": "%u"` (the `%u`
  sits inside literal quotes), so you get `"id": "73"`. Cast with
  `int(obj["id"])` if you need a number.
- **No `"current"`/`"default"`/`"selected"` key in `-a` JSON output.** To find
  which device is active you must separately call `-c`.
- **All diagnostics go to stdout, not stderr.** The tool uses `printf`
  throughout (in `audio_switch.c`). "Could not find…", "Please specify audio
  device.", "Unknown format", "Invalid device type", and mute failures all
  land on stdout. When integrating, inspect `proc.stdout` on failure.
- **Unknown device name → exit code 1** with message
  `Could not find an audio device named "<name>" of type <type>.  Nothing was changed.`
  (state is left unchanged). Unknown UID uses the analogous `with UID "<uid>"`
  wording.
- **Name matching is exact (`strcmp`).** `-s` requires the full device name
  verbatim; `-u` is a substring match (`strstr`) and is more forgiving; `-i`
  is an integer id (`atoi`).
- **A CoreAudio set failure is masked as success.** `setOneDevice()` prints
  `Failed to set <type>` on error but **always returns 0**, and the top-level
  then prints `<type> audio device set to "<name>"` and returns 0. A 0 exit
  does not guarantee the device actually changed; verify with a follow-up `-c`
  if it matters.
- **`-t system` cannot be muted.** `-m ... -t system` prints
  `audio device "system" may not be muted` and returns 1. Use `-t input` or
  `-t output` (or `-t all` for both).
- **Action-flag precedence.** Only one action runs per invocation: `-a`, `-c`,
  `-h`, `-n`, and the set actions (`-i`/`-u`/`-s`/`-m`) all assign to the same
  `function` variable inside the option loop, so if you pass two, the **last
  one wins**. Don't combine e.g. `-a -c`.
- **`-t` defaults to `output`**, so bare `SwitchAudioSource -c` reports the
  output device and bare `SwitchAudioSource -s "X"` switches the output
  device — you must add `-t input` explicitly for input operations.
- **`requestedDeviceName`/`requestedDeviceUID` buffers are 256 bytes**
  (`strcpy` with no bounds check in source). Realistically not a constraint,
  but do not pass pathological multi-KB strings.
- **macOS-only** (CoreAudio/CoreServices). Not portable to Linux/Windows.
- Requires the binary on `PATH`. On macOS it is typically installed via
  Homebrew (`brew install switchaudio-osx`), which provides the
  `SwitchAudioSource` executable.

---

## 3. python-sounddevice

Source versions: latest `readthedocs.io` API pages plus the canonical source at
`_modules/sounddevice.html`. Bindings for the **PortAudio** library;
convenience functions to play/record NumPy arrays.

### Import

```python
import sounddevice as sd
```

Always `import numpy` *before* starting a stream if the callback uses NumPy
(the official `rec_unlimited.py` example does `import numpy` /
`assert numpy` so NumPy is loaded before the callback thread first runs).

### Core API

**`sd.InputStream`** — input-only stream using NumPy arrays. Constructor
signature (identical to `sd.Stream` minus the output buffer):

```python
class sounddevice.InputStream(
    samplerate=None,
    blocksize=None,
    device=None,
    channels=None,
    dtype=None,
    latency=None,
    extra_settings=None,
    callback=None,
    finished_callback=None,
    clip_off=None,
    dither_off=None,
    never_drop_input=None,
    prime_output_buffers_using_stream_callback=None,
)
```

Parameter meanings (from the docs):

| Parameter | Type | Notes |
|---|---|---|
| `samplerate` | float, optional | Desired sampling frequency (Hz). |
| `blocksize` | int, optional | Frames passed to the callback per call. `0`/`None` lets PortAudio pick. |
| `device` | int or str or pair thereof, optional | Device index(es) or a query-string substring (see Device selection). |
| `channels` | int or pair of int, optional | Number of channels delivered to the callback. |
| `dtype` | str / `numpy.dtype` / pair, optional | Sample format of the ndarray. One of `'float32'`, `'int32'`, `'int16'`, `'int8'`, `'uint8'`. |
| `latency` | float or `{'low','high'}` or pair, optional | Desired latency in seconds. |
| `extra_settings` | settings object or pair, optional | Platform-specific settings, e.g. `CoreAudioSettings(...)`. |
| `callback` | callable, optional | `callback(indata, frames, time, status) -> None`. |
| `finished_callback` | callable, optional | Called when the stream becomes inactive. |
| `clip_off` | bool, optional | Disable clipping. |
| `dither_off` | bool, optional | Disable dithering. |
| `never_drop_input` | bool, optional | |
| `prime_output_buffers_using_stream_callback` | bool, optional | (output-stream relevant) |

**Stream callback signature** (input stream):

```python
def callback(indata: numpy.ndarray, frames: int, time: CData, status: CallbackFlags) -> None
```

- `indata` — 2-D `numpy.ndarray` of **shape `(frames, channels)`** (frames
  major, channels minor) and dtype determined by the stream's `dtype` argument
  (default `'float32'`, range -1.0..1.0).
- `frames` — int, number of frames in this block (== `indata.shape[0]`).
- `time` — CFFI `CData` struct with float (seconds) attributes:
  `time.inputBufferAdcTime` (ADC capture time of first input sample),
  `time.outputBufferDacTime`, `time.currentTime` (callback invocation time).
- `status` — `CallbackFlags` (see below).

**`CallbackFlags`** — the `status` argument. Boolean attributes (confirmed from
the API index):

| Flag | Meaning | What to do |
|---|---|---|
| `input_underflow` | Input data was inserted (no data available). | Non-fatal; usually log/ignore. Indicates the device/latency can't keep up. |
| `input_overflow` | Previously recorded input data was dropped. | Non-fatal; log. Means your callback is consuming too slowly — increase `blocksize`/`latency`. |
| `output_underflow` | Output data was inserted (silence). | Input-stream only: ignore. |
| `output_overflow` | Output data was dropped. | Input-stream only: ignore. |
| `priming_output` | Callback is being called to prime the output buffers (initial call). | Ignore. |

Raise `sd.CallbackStop` inside the callback to stop gracefully; raise
`sd.CallbackAbort` to stop immediately. An uncaught exception stops the
callback and prints the traceback to `sys.stderr`.

**`sd.query_devices([device[, kind]])`** — returns a `DeviceList` (list-like;
not meant to be user-instantiated). Each entry is a dict:

| Key | Type |
|---|---|
| `name` | str — device name |
| `index` | int — device index |
| `hostapi` | int — ID of the host API (index into `query_hostapis()`) |
| `max_input_channels` | int |
| `max_output_channels` | int |
| `default_low_input_latency` | float |
| `default_low_output_latency` | float |
| `default_high_input_latency` | float |
| `default_high_output_latency` | float |
| `default_samplerate` | float |

- Passing an int returns that single device's dict; passing a string returns
  the first device whose name contains the substring.
- `kind='input'` / `kind='output'` returns the default input/output device.
- **Input-only** device: `max_input_channels > 0` and
  `max_output_channels == 0`. **Output-only**: the reverse. (BlackHole 2ch
  appears with `max_input_channels == 2` and `max_output_channels == 2` — it
  is a loopback, so both are non-zero.)
- `sd.query_hostapis()` returns host-API dicts with `name`, `devices` (list of
  device indices), `default_input_device`, `default_output_device` (`-1` if
  none).

**`sd.default`** — mutable module-level default settings:

- `sd.default.device` — `[input_index, output_index]` list. Default is
  `[-1, -1]` (PortAudio picks). Set with
  `sd.default.device = [blackhole_index, None]` or a single int (sets both).
- `sd.default.samplerate` — default sample rate (None initially).
- `sd.default.channels` — int or `[in, out]` pair, e.g.
  `sd.default.channels = 2` or `[2, 2]`.
- `sd.default.dtype` — default ndarray dtype (`'float32'`).
- `sd.default.latency` — default latency.
- `sd.default.extra_settings` — default platform-specific settings object.

**`CoreAudioSettings`** — YES, exists (macOS Core Audio). Exact source
signature:

```python
class sounddevice.CoreAudioSettings(
    channel_map=None,
    change_device_parameters=False,
    fail_if_conversion_required=False,
    conversion_quality='max',
)
```

- `channel_map` — list/sequence remapping channels.
- `change_device_parameters` — bool, allow modifying device frame size for
  lower latency.
- `fail_if_conversion_required` — bool, fail stream open if the device won't
  honour the requested sample rate exactly (useful for catching macOS
  sample-rate mismatches instead of silent conversion).
- `conversion_quality` — one of `'min'`, `'low'`, `'medium'`, `'high'`,
  `'max'`.
- Pass via
  `sd.InputStream(device=..., extra_settings=sd.CoreAudioSettings(...), ...)`.

### Recording pattern (InputStream + callback)

Canonical "Recording with Arbitrary Duration" pattern (verbatim logic from the
official `rec_unlimited.py`): callback copies each block onto a `queue.Queue`;
the main thread drains the queue and writes to a `soundfile.SoundFile`. Crucial
detail: `q.put(indata.copy())` — the `indata` buffer is reused by PortAudio,
so you **must copy**.

```python
import argparse
import queue
import sys

import numpy  # Make sure NumPy is loaded before it's used in the callback
assert numpy  # Avoid "imported but unused" warning

import sounddevice as sd
import soundfile as sf

q = queue.Queue()

def callback(indata, frames, time, status):
    """This is called (from a separate thread) for each audio block."""
    if status:
        print(status, file=sys.stderr)
    q.put(indata.copy())


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('-d', '--device', type=int)
    parser.add_argument('-r', '--samplerate', type=float)
    parser.add_argument('-c', '--channels', type=int, default=1)
    parser.add_argument('filename', nargs='?', metavar='FILENAME',
                        default='rec.wav')
    args = parser.parse_args()

    try:
        import sounddevice as sd
        import soundfile as sf

        if args.samplerate is None:
            device_info = sd.query_devices(args.device, 'input')
            args.samplerate = device_info['default_samplerate']

        # Make sure the file is opened before recording anything,
        # so no data is lost.
        with sf.SoundFile(args.filename, mode='x', samplerate=int(args.samplerate),
                          channels=args.channels, subtype=args.subtype) as file:
            with sd.InputStream(samplerate=args.samplerate, device=args.device,
                                channels=args.channels, callback=callback):
                print('#' * 80)
                print('press Ctrl+C to stop the recording')
                print('#' * 80)
                while True:
                    file.write(q.get())

    except KeyboardInterrupt:
        print('\nRecording finished: ' + repr(args.filename))
        parser.exit(0)
    except Exception as e:
        parser.exit(type(e).__name__ + ': ' + str(e))
```

`indata` reaching the callback is shape `(blocksize, channels)`, dtype per the
stream's `dtype` (default `float32`). `soundfile.SoundFile(..., subtype=...)`
must match: `float32` ndarray -> `subtype='FLOAT'`; for 16-bit PCM, set
`dtype='int16'` on the stream **and** `subtype='PCM_16'` on the file.
Mismatching them produces silence or clipped data.

### Device selection

By integer index (most explicit; what you should use for BlackHole):

```python
import sounddevice as sd

# Find BlackHole's input index by name
blackhole_idx = None
for i, dev in enumerate(sd.query_devices()):
    if 'BlackHole' in dev['name'] and dev['max_input_channels'] > 0:
        blackhole_idx = i
        break

stream = sd.InputStream(device=blackhole_idx, channels=2, samplerate=48000, callback=cb)
```

By string query (PortAudio matches the first device containing the substring):

```python
sd.InputStream(device='BlackHole', channels=2, ...)            # str
sd.InputStream(device=('BlackHole', 'BlackHole'), ...)         # pair for full-duplex Stream
```

Or set the global default once:

```python
sd.default.device = blackhole_idx          # sets both in/out
sd.default.device = [blackhole_idx, None]  # in=BlackHole, out=unchanged
sd.InputStream(channels=2, ...)            # device=None -> uses default
```

`query_devices(device, kind='input')` returns the default-input device's dict;
`kind` accepts `'input'` or `'output'`.

### Gotchas

- **Callback must not block.** It runs on a high-priority PortAudio thread. Do
  **not** allocate memory, touch the filesystem, acquire locks with
  contention, or call PortAudio/sounddevice stream-control APIs from it. The
  canonical fix is `q.put(indata.copy())` and do all disk I/O on the main
  thread.
- **Copy the buffer.** `indata` is reused by PortAudio; passing it to a queue
  without `.copy()` yields corrupted/garbage audio.
- **dtype must match across stream, file, and code.** `sd.InputStream(dtype='int16')`
  pairs with `sf.SoundFile(subtype='PCM_16')`. Default dtype is `float32`
  (range -1..1), which pairs with `subtype='FLOAT'`. Writing `float32` data to
  a `PCM_16` file does *not* auto-scale correctly and yields clipped output.
- **Sample-rate mismatches on macOS.** BlackHole and the output device (e.g.
  speakers/headphones) must run at the same sample rate, or AudioMIDI Setup
  will resample. If sample rates differ, set them equal in AudioMIDI Setup, or
  pass `extra_settings=sd.CoreAudioSettings(fail_if_conversion_required=True)`
  to fail loud instead of silently resampling. Query the device's native rate
  via `sd.query_devices(idx)['default_samplerate']`.
- **Keep a reference to the stream.** The `with sd.InputStream(...) as stream:`
  form keeps it alive; assigning to a local that goes out of scope can stop the
  stream. Use the context manager or hold the object until you call
  `.stop()`/`.close()`.
- **KeyboardInterrupt.** Catch it on the main thread (as in the example); the
  `with` blocks then close the stream and file cleanly. Do not raise it from
  the callback.
- **NumPy import ordering.** Import NumPy before the callback thread starts;
  the official example imports it explicitly and asserts to keep it loaded.
- **Thread-safety of stream-control calls.** Don't call `stream.start()`/
  `stream.stop()` etc. from inside the callback; raise `sd.CallbackStop`
  instead.
- **macOS / Apple Silicon notes.** sounddevice bundles a pre-built PortAudio
  binary and works natively on arm64 via wheels (no separate install of
  PortAudio needed). BlackHole (a virtual loopback) shows up as a device with
  both `max_input_channels` and `max_output_channels` non-zero. Permissions:
  macOS may require microphone/input access; if `query_devices()` returns an
  empty list or the device is missing, check System Settings -> Privacy &
  Security and ensure the terminal/app has audio-input permission. Long device
  names can be truncated by PortAudio (GitHub issue #307), so match on a
  substring (e.g. `'BlackHole'`) rather than the full name.

---

## 4. python-soundfile

### Import

```python
import soundfile as sf
```

`soundfile` is an audio library based on `libsndfile` + CFFI + NumPy. Audio
data is represented as NumPy arrays.

### Core API (SoundFile class)

**Constructor** (version 0.13.x):

```python
soundfile.SoundFile(file, mode='r', samplerate=None, channels=None,
                    subtype=None, endian=None, format=None, closefd=True,
                    compression_level=None, bitrate_mode=None)
```

Defaults: `mode='r'`; `samplerate`, `channels`, `subtype`, `endian`, `format`
all default to `None`. When **reading** (`'r'`/`'r+'`) these are auto-detected
from the file; when **writing** (`'w'`/`'w+'`/`'x'`/`'x+'`) you **must** provide
`samplerate` and `channels`.

Mode behaviors:

- `'r'` — read only.
- `'r+'` — read and write (existing file).
- `'w'` — write only, **truncates/overwrites** the file.
- `'w+'` — read+write, truncates the file.
- `'x'` / `'x+'` — write / read+write, **raises if the file already exists**.

**`SoundFile.write(data)`** — accepts `array_like` (a NumPy array). Shape
conventions:

- 1D array `(frames,)` → mono (interleaved single channel).
- 2D array `(frames, channels)` → multi-channel, one column per channel.

Accepted dtypes are limited to **`'float64'`, `'float32'`, `'int32'`,
`'int16'`**. The dtype of the NumPy array does **not** select the on-disk
format; data is converted by libsndfile to the file's declared `subtype`.

**`SoundFile.close()`** — closes the file; safe to call multiple times. When
used as a context manager (`with sf.SoundFile(...) as f:`) `close()` is invoked
automatically at block exit. The file is finalized/flushed on close (header
updated, buffer written to disk). **You cannot `write()` after `close()`** — it
raises.

**`SoundFile.flush()`** — forces any in-memory buffered changes to be written
to the filesystem. Useful when another process (or a tailing reader) needs to
see partial data before close.

**Attributes** (read on the opened object):

- `f.samplerate` — sample rate in Hz.
- `f.channels` — number of channels.
- `f.frames` — total number of frames (discrete time-steps) currently in the
  file.
- `f.subtype` — the active subtype string (e.g. `'PCM_16'`).
- Also: `f.format`, `f.endian`, `f.name`, `f.mode`, `f.closed`.

### Streaming write pattern

Open the file **once**, call `f.write(chunk)` repeatedly with each incoming
buffer, then close once. The write pointer advances and the file grows as you
write. **Thread safety caveat:** soundfile writes are **not** safe to call
concurrently — it is explicitly documented that *"it is not safe to
concurrently write to the same file or share reader or writer handles."* If
your audio callback fires on a background thread (e.g.
`sounddevice.InputStream`), serialize the writes from that single thread (do
not call `f.write()` from two threads); if your design needs handoff, queue
the chunks onto one writer thread that owns the `SoundFile`.

```python
import numpy as np
import soundfile as sf

# PCM_16 expects int16 data. Match dtype to subtype exactly to avoid
# silent scaling/conversion or clipping.
path      = "meeting.wav"
samplerate = 16000
channels   = 1
subtype    = "PCM_16"     # -> write int16 numpy chunks
dtype      = np.int16     # MUST match the subtype

with sf.SoundFile(path, mode="w", samplerate=samplerate,
                  channels=channels, subtype=subtype) as f:
    while recording:
        chunk = get_next_chunk()           # np.ndarray, shape (frames,) for mono
        if chunk.dtype != dtype:
            chunk = chunk.astype(dtype)    # explicit conversion
        f.write(chunk)                     # repeated streaming writes
    # f.close() is called automatically here -> header flushed, file finalized

# ---- float32 alternative (subtype must be FLOAT, not PCM_16) ----
# with sf.SoundFile(path, mode="w", samplerate=16000, channels=1,
#                   subtype="FLOAT") as f:
#     f.write(chunk.astype(np.float32))    # shape (frames,), values in [-1.0, 1.0)
```

For one-shot writes (whole array already in memory) use the convenience
function instead:

```python
soundfile.write(file, data, samplerate, subtype=None, endian=None,
                format=None, closefd=True, compression_level=None,
                bitrate_mode=None)
```

It truncates/overwrites an existing file and closes automatically. Use
`sf.write()` when you have the full recording; use `SoundFile` (`mode='w'`)
for **streaming** so you don't hold the entire recording in RAM.

### Formats and subtypes

`sf.available_subtypes(format=None)` returns a dict of compatible subtype
strings for a format. The **major format** is auto-detected from the filename
extension (`.wav` → `WAV`, `.flac` → `FLAC`, `.ogg` → `OGG`, etc.) unless
overridden by the `format=` argument (e.g. `format='WAVEX'` for >4 GB WAV).

`sf.available_subtypes('WAV')` returns (string → description, plus numeric
info), including:

| Subtype string | Description | Bit depth | Native dtype |
|---|---|---|---|
| `PCM_S8` | Signed 8 bit | 8 | `int16` (scaled) |
| `PCM_U8` | Unsigned 8 bit (WAV/RAW only) | 8 | `int16` |
| `PCM_16` | Signed 16 bit | 16 | `int16` |
| `PCM_24` | Signed 24 bit | 24 | `int32` (padded) |
| `PCM_32` | Signed 32 bit | 32 | `int32` |
| `FLOAT` | 32 bit float | 32 | `float32` |
| `DOUBLE` | 64 bit float | 64 | `float64` |
| `ULAW` | U-Law encoded | — | `int16` / float |
| `ALAW` | A-Law encoded | — | `int16` / float |
| `IMA_ADPCM` | IMA ADPCM | — | `int16` |
| `MS_ADPCM` | Microsoft ADPCM | — | `int16` |
| `GSM610` | GSM 6.10 | — | `int16` |
| `VOX_ADPCM` | OKI / Dialogix ADPCM | — | `int16` |

`sf.available_formats()` lists all major formats (`WAV`, `FLAC`, `OGG`,
`AIFF`, `RAW`, `MAT4`, `MAT5`, etc.).

### Gotchas

- **dtype must match subtype.** The NumPy dtype is converted by libsndfile to
  the file's `subtype`; it does **not** determine the file type. Writing
  `np.array([42], dtype='int32')` to a `subtype='FLOAT'` file stores
  `np.array([42.], dtype='float32')`. Match them to avoid unintended
  conversion: `PCM_16`→`int16`, `PCM_24`→`int32`, `FLOAT`→`float32`,
  `DOUBLE`→`float64`.
- **`PCM_24` is tricky.** libsndfile stores 24-bit audio in a 32-bit
  container, so write **`int32`** arrays (not int24 — that doesn't exist in
  NumPy). Low 8 bits are ignored on write.
- **Writing float32 to `PCM_16` silently clips.** Values outside `[-1.0, 1.0)`
  are clipped (not rescaled) — you can clip/distort without any exception.
  Keep float data normalized to `[-1.0, 1.0)`.
- **Reading int values from a float file is not scaled** to `[-1.0, 1.0)` —
  you get the raw converted integers.
- **`write()` after `close()` raises.** Use the context manager or call
  `close()` exactly once at the end.
- **No thread safety.** Concurrent writes to the same `SoundFile` are unsafe;
  keep all `write()` calls on one (the recording) thread.
- **Flush before another process reads.** Until `close()` (or `flush()`), the
  header may not reflect all frames; tailing readers should not assume a
  finalized file until close.
- **`sf.write()` truncates** an existing file without warning — streaming with
  `SoundFile` avoids clobbering.

---

## 5. faster-whisper

`faster-whisper` is a Python reimplementation of OpenAI Whisper built on
**CTranslate2**. All signatures below were verified directly against
`faster_whisper/transcribe.py` and `faster_whisper/vad.py` on the `master`
branch (the README's listed signature is slightly out of date — the source
adds several newer params). Model-size/VRAM numbers come from OpenAI's Whisper
README (faster-whisper uses identical weights).

### Import

```python
from faster_whisper import WhisperModel
# For batched inference:
from faster_whisper import BatchedInferencePipeline
```

### WhisperModel constructor

Verified against `WhisperModel.__init__` in `faster_whisper/transcribe.py`:

```python
def __init__(
    self,
    model_size_or_path: str,
    device: str = "auto",
    device_index: Union[int, List[int]] = 0,
    compute_type: str = "default",
    cpu_threads: int = 0,
    num_workers: int = 1,
    download_root: Optional[str] = None,
    local_files_only: bool = False,
    files: dict = None,
    revision: Optional[str] = None,
    use_auth_token: Optional[Union[str, bool]] = None,
    **model_kwargs,
)
```

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `model_size_or_path` | `str` | (required) | Size name (`"large-v3"`) or path to a local CTranslate2 model dir. |
| `device` | `str` | `"auto"` | `"cpu"`, `"cuda"`, `"auto"`. On Apple Silicon use `"cpu"` (no Metal backend). |
| `device_index` | `int` or `List[int]` | `0` | Device ordinal(s); list enables data parallelism. |
| `compute_type` | `str` | `"default"` | `"default"` resolves to `int8` on CPU. Use `"int8"` explicitly on Apple Silicon for speed. Also: `int8_float32`, `int8_float16`, `float16`, `float32`, `auto`, `bfloat16`. |
| `cpu_threads` | `int` | `0` | `0` = use default (all cores). Set explicitly to pin a thread count. |
| `num_workers` | `int` | `1` | Workers for multi-file/dataloader-style use; leave `1` for CLI transcription. |
| `download_root` | `Optional[str]` | `None` | Override cache dir; defaults to HF cache (`~/.cache/huggingface/hub`). |
| `local_files_only` | `bool` | `False` | `True` → never hit network; load only from cache (use after first run for offline). |
| `files` | `dict` | `None` | In-memory model files map. |
| `revision` | `Optional[str]` | `None` | HF model revision/commit. |
| `use_auth_token` | `Optional[Union[str, bool]]` | `None` | HF auth token for gated models. |
| `**model_kwargs` | — | — | Passed through to CTranslate2. |

> **NOTE:** the task description's signature omitted `files`, `revision`,
> `use_auth_token`, and `**model_kwargs` — those DO exist in the real source.

### transcribe() full signature

Verified verbatim against `WhisperModel.transcribe` in
`faster_whisper/transcribe.py`. The README you quoted is **incomplete** — the
source adds `log_progress`, `multilingual`,
`language_detection_threshold`, and `language_detection_segments`, and the
README's `beam_size_realtime` parameter does **NOT** exist in
`WhisperModel.transcribe` (verify in source — `beam_size_realtime` is not a
parameter of the public `transcribe`; treat it as not-present).

Return type: `Tuple[Iterable[Segment], TranscriptionInfo]` — `segments` is a
**lazy generator**, so you must iterate it (or materialize via `list`) before
`info` fields that depend on it are finalized; always consume the generator.

```python
def transcribe(
    audio: Union[str, BinaryIO, np.ndarray],
    language: Optional[str] = None,
    task: str = "transcribe",                      # or "translate"
    log_progress: bool = False,
    beam_size: int = 5,
    best_of: int = 5,
    patience: float = 1,
    length_penalty: float = 1,
    repetition_penalty: float = 1,
    no_repeat_ngram_size: int = 0,
    temperature: Union[float, List[float], Tuple[float, ...]] = [
        0.0, 0.2, 0.4, 0.6, 0.8, 1.0
    ],
    compression_ratio_threshold: Optional[float] = 2.4,
    log_prob_threshold: Optional[float] = -1.0,
    no_speech_threshold: Optional[float] = 0.6,
    condition_on_previous_text: bool = True,
    prompt_reset_on_temperature: float = 0.5,
    initial_prompt: Optional[Union[str, Iterable[int]]] = None,
    prefix: Optional[str] = None,
    suppress_blank: bool = True,
    suppress_tokens: Optional[List[int]] = [-1],
    without_timestamps: bool = False,
    max_initial_timestamp: float = 1.0,
    word_timestamps: bool = False,
    prepend_punctuations: str = "\"'"¿([{-",
    append_punctuations: str = "\"'.。，！？：\")]}、",
    multilingual: bool = False,
    vad_filter: bool = False,
    vad_parameters: Optional[Union[dict, VadOptions]] = None,
    max_new_tokens: Optional[int] = None,
    chunk_length: Optional[int] = None,
    clip_timestamps: Union[str, List[float]] = "0",
    hallucination_silence_threshold: Optional[float] = None,
    hotwords: Optional[str] = None,
    language_detection_threshold: Optional[float] = 0.5,
    language_detection_segments: int = 1,
) -> Tuple[Iterable[Segment], TranscriptionInfo]:
```

Key param notes:

- `audio` — a **path string**, a file-like `BinaryIO`, or a `np.ndarray`
  (float32 mono, 16 kHz). A path to a `.wav` works directly (decoded via
  `av`/PyAV).
- `language` — ISO-639-1 code (e.g. `"en"`, `"es"`). `None` → auto-detected;
  the detection is reported in `info.language` / `info.language_probability`.
  Set it explicitly to skip detection and improve speed/accuracy.
- `task` — `"transcribe"` (default) or `"translate"` (translate to English).
- `temperature` — list of fallback temperatures tried in order (OpenAI's
  "temperature fallback" — if a segment fails thresholds it retries hotter).
- `compression_ratio_threshold` / `log_prob_threshold` / `no_speech_threshold`
  — segments exceeding these are treated as failed/hallucinated and retried.
  The defaults (`2.4`, `-1.0`, `0.6`) occasionally **drop** quiet CJK segments;
  raise `no_speech_threshold` if you lose content.
- `condition_on_previous_text` — `True` feeds the previous segment as context;
  can cause hallucination loops on long silences. Consider `False` for clean
  long-form audio.
- `vad_filter` — `False` by default. Set `True` to run Silero VAD first and
  skip silence.
- `word_timestamps` — `False` by default. `True` populates `segment.words`
  (uses cross-attention).
- `hallucination_silence_threshold` — seconds; silence longer than this in
  non-speech gaps is suppressed (helps the "Thank you." repetition
  hallucination). `None` = off.
- `hotwords` — string of words to bias toward in decoding.
- `language_detection_threshold` / `language_detection_segments` — control
  auto-language-detection gating (newer params; verify in source if pinning an
  old version).

### Segment and TranscriptionInfo structures

Both are **`@dataclass`** (not `NamedTuple`), verified in source:

```python
@dataclass
class Segment:
    id: int
    seek: int
    start: float
    end: float
    text: str
    tokens: List[int]
    avg_logprob: float
    compression_ratio: float
    no_speech_prob: float
    words: Optional[List[Word]]
    temperature: Optional[float]
```

```python
@dataclass
class Word:
    start: float
    end: float
    word: str
    probability: float
```

```python
@dataclass
class TranscriptionInfo:
    language: str
    language_probability: float
    duration: float
    duration_after_vad: float
    all_language_probs: Optional[List[Tuple[str, float]]]
    transcription_options: TranscriptionOptions
    vad_options: VadOptions
```

Notes:

- `Segment.words` is `None` unless `word_timestamps=True`.
- `Segment.temperature` reflects the temperature actually used (after
  fallback) for that segment.
- `info.duration_after_vad` is meaningful only when `vad_filter=True`;
  otherwise it equals `duration`.
- `info.all_language_probs` is `None` unless the autodetection kept the full
  distribution.

### VAD and word timestamps

VAD uses **Silero VAD**. `vad_parameters` accepts a `dict` or a `VadOptions`.
Verified defaults from `faster_whisper/vad.py`:

```python
@dataclass
class VadOptions:
    threshold: float = 0.5
    neg_threshold: float = None
    min_speech_duration_ms: int = 0
    max_speech_duration_s: float = float("inf")
    min_silence_duration_ms: int = 2000
    speech_pad_ms: int = 400
    min_silence_at_max_speech: int = 98
    use_max_poss_sil_at_max_speech: bool = True
```

| Key | Default | Meaning |
|---|---|---|
| `threshold` | `0.5` | Speech prob above this = speech. Lower = more aggressive speech detection. |
| `neg_threshold` | `None` | Noise-reduction threshold for exiting speech. |
| `min_speech_duration_ms` | `0` | Drop speech runs shorter than this. |
| `max_speech_duration_s` | `inf` | Force-split speech longer than this (used by BatchedInferencePipeline). |
| `min_silence_duration_ms` | `2000` | Silence ≥ 2 s splits segments. Commonly lowered to `500` for tighter cuts. |
| `speech_pad_ms` | `400` | Pad each speech chunk by this much on both sides. |
| `min_silence_at_max_speech` | `98` | Min trailing silence retained at a forced split. |
| `use_max_poss_sil_at_max_speech` | `True` | Pad forced splits with the longest possible silence. |

Usage:

```python
segments, info = model.transcribe(
    "audio.wav",
    vad_filter=True,
    vad_parameters=dict(min_silence_duration_ms=500, threshold=0.4),
)
```

`vad_parameters` is **ignored unless `vad_filter=True`**.

Word timestamps — set `word_timestamps=True`; each `segment.words` is a list of
`Word(start, end, word, probability)`:

```python
segments, info = model.transcribe("audio.wav", word_timestamps=True)
for seg in segments:
    for w in seg.words:
        print(w.start, w.end, w.word)
```

### Model sizes and tradeoffs

faster-whisper loads the **same weights** as OpenAI Whisper (CTranslate2-
converted repos under `Systran/faster-whisper-*` on Hugging Face). VRAM/speed
figures below are OpenAI's reference numbers (A100 GPU); on CPU the absolute
speed differs but the **relative ordering holds**. INT8 quantization roughly
halves RAM vs the float numbers below.

| Size | Params | Multilingual name | VRAM (float) | Rel. speed | Notes |
|---|---|---|---|---|---|
| tiny | 39 M | `tiny` | ~1 GB | ~10x | Fastest, lowest accuracy. |
| base | 74 M | `base` | ~1 GB | ~7x | |
| small | 244 M | `small` | ~2 GB | ~4x | Good speed/accuracy balance. |
| medium | 769 M | `medium` | ~5 GB | ~2x | |
| large | 1550 M | `large-v2` / `large-v3` | ~10 GB | 1x | Best accuracy. |
| large-v1 | 1550 M | `large-v1` | ~10 GB | 1x | Original large. |
| large-v2 | 1550 M | `large-v2` | ~10 GB | 1x | Improved training; widely used. |
| large-v3 | 1550 M | `large-v3` | ~10 GB | 1x | Current default recommendation. |
| distil-large-v2/v3 | ~756 M | `distil-large-v2`, `distil-large-v3` | ~5 GB | ~2x faster than large | Distilled; English-only, ~99% of large accuracy. |
| turbo | ~809 M | `turbo` | ~6 GB | ~3x | `large-v3`-accuracy, faster. |

`.en` English-only variants (`tiny.en`, `base.en`, etc.) exist for tiny→medium
and are slightly better on English-only audio; `large` has no `.en`.

Recommended: `large-v3` for best accuracy; `small` or `medium` when you need
speed on CPU. For an Apple Silicon meeting-recorder CLI, `medium` is a
reasonable default and `large-v3` for high-quality English.

### CPU compute types

- `int8` — **fastest on CPU**; lowest RAM. Recommended for Apple Silicon.
- `int8_float32` — int8 weights, float32 compute; for mixed setups.
- `float32` — full precision; slower, more RAM; useful for max-accuracy CPU
  runs.
- `default` — resolves to `int8` on CPU automatically (this is the constructor
  default).
- `auto` — lets CTranslate2 choose (typically resolves like `default`).

`float16` / `int8_float16` / `bfloat16` require a GPU (CUDA) and are **not
usable on Apple Silicon**. Use `compute_type="int8"` (or rely on the
`"default"` default) on a CPU-only Mac.

### BatchedInferencePipeline

Verified against `faster_whisper/transcribe.py`:

```python
class BatchedInferencePipeline:
    def __init__(self, model):          # model: a WhisperModel instance
        self.model: WhisperModel = model
        self.last_speech_timestamp = 0.0
```

Usage:

```python
from faster_whisper import WhisperModel, BatchedInferencePipeline
model = WhisperModel("large-v3", device="cpu", compute_type="int8")
batched = BatchedInferencePipeline(model=model)
segments, info = batched.transcribe("audio.wav", batch_size=16)
```

Key differences vs `WhisperModel.transcribe` (verified):

- Adds `batch_size: int = 8` — max parallel decode requests (the "12x faster"
  claim is GPU-bound; CPU still benefits modestly).
- `vad_filter` defaults to **`True`** (vs `False` in `WhisperModel`).
- `without_timestamps` defaults to **`True`** (timestamps reconstructed
  after).
- `clip_timestamps` type is `Optional[List[dict]]`
  (vs `Union[str, List[float]]`).
- `hallucination_silence_threshold` is accepted but **unused** (no-op) in
  batched mode.
- Internally forces `vad_parameters` `max_speech_duration_s = chunk_length` and
  `min_silence_duration_ms = 160`.

For a single long WAV recording on CPU, plain `WhisperModel.transcribe` is
usually sufficient and has finer control; `BatchedInferencePipeline` shines
when you have many files or a GPU.

### Model download and caching

- Passing a size name (`WhisperModel("large-v3")`) auto-downloads the
  CTranslate2-converted weights from the `Systran/faster-whisper-large-v3`
  (etc.) Hugging Face repos on **first run**.
- Default cache location: `~/.cache/huggingface/hub/` (the HF Hub cache).
  Override via the `download_root=` constructor arg or the
  `HUGGINGFACE_HUB_CACHE` / `HF_HOME` env var.
- First run **requires internet** (downloads from Hugging Face). After that,
  pass `local_files_only=True` to run fully offline.
- Approximate download sizes (CTranslate2 float16 repos; int8 variants are
  ~half):
  - `tiny` ~ 75 MB, `base` ~ 145 MB, `small` ~ 480 MB, `medium` ~ 1.5 GB,
    `large-v3` ~ 3 GB.
  - Pre-quantized int8 community repos (e.g.
    `groxaxo/faster-whisper-large-v3-int8-ct2`) are ~1.5 GB but note:
    faster-whisper quantizes on the fly when you pass `compute_type="int8"` to
    a float16 repo, so a separate int8 repo is not required.

### Performance notes (Apple Silicon)

- faster-whisper is built on **CTranslate2**, which has **no Metal/MPS
  backend**. There is **no native GPU acceleration** on Apple Silicon. Tracked
  at [issue #515](https://github.com/SYSTRAN/faster-whisper/issues/515).
- All inference runs on **CPU**, accelerated via macOS's **Accelerate
  Framework** (multi-core + SIMD).
- Use `device="cpu", compute_type="int8"` (or just `compute_type="default"`,
  which resolves to `int8` on CPU). `device="cuda"`, `float16`, and
  `int8_float16` will not work.
- Expect faster-whisper to be **significantly faster than vanilla OpenAI
  Whisper** on the same CPU (CTranslate2 + int8), but slower than
  whisper.cpp/MLX-Whisper which can use the Metal GPU.
- For a 1-hour recording: `large-v3` int8 on an M-series chip is typically
  several minutes (a fraction of realtime with VAD enabled); `medium`/`small`
  are markedly faster.

### Gotchas

- **`segments` is a lazy generator.** Always iterate it (e.g.
  `list(segments)` or a `for` loop) — otherwise transcription never actually
  runs and `info` may be incomplete. Do not just call `transcribe()` and read
  `info`.
- The README's advertised `beam_size_realtime` parameter **does not exist** in
  `WhisperModel.transcribe` (verify in source for your version) — drop it.
- The README signature omits `log_progress`, `multilingual`,
  `language_detection_threshold`, `language_detection_segments`, and the
  constructor's `files`/`revision`/`use_auth_token`/`**model_kwargs`.
- `compression_ratio_threshold` (2.4), `log_prob_threshold` (-1.0), and
  `no_speech_threshold` (0.6) can silently **drop** quiet CJK/silent segments
  — tune or disable if you lose content.
- `condition_on_previous_text=True` can trigger **hallucination loops**
  ("Thank you." repeating) on long silences; consider `False` plus
  `hallucination_silence_threshold=2.0`.
- First run downloads weights — requires internet and several GB free for
  large models. Set `local_files_only=True` only after the first successful
  run, or it raises.
- `vad_parameters` is **ignored unless `vad_filter=True`** (except in
  `BatchedInferencePipeline`, where VAD is on by default).
- On Apple Silicon do not pass `float16`/`int8_float16`/`bfloat16`/
  `device="cuda"` — they will error.
- Segment `text` is already stripped/trailing-newline-trimmed; it does not
  include a leading space between segments.

### Transcription pattern (minimal example)

Copy-pasteable for a CPU-only Apple Silicon Mac, loading int8, transcribing a
WAV, and printing start/end/text:

```python
from faster_whisper import WhisperModel

# int8 on CPU — the right combo for Apple Silicon / CPU-only Macs.
model = WhisperModel(
    "large-v3",                 # or "medium" / "small" for speed
    device="cpu",
    compute_type="int8",
    cpu_threads=0,              # 0 = use all cores
)

# transcribe() returns (lazy generator, info)
segments, info = model.transcribe(
    "recording.wav",
    language="en",              # set explicitly to skip auto-detection
    beam_size=5,
    vad_filter=True,            # skip long silences
    vad_parameters=dict(min_silence_duration_ms=500),
)

print(f"Detected language: {info.language} (p={info.language_probability:.2f})")
print(f"Duration: {info.duration:.1f}s")

# MUST iterate the generator to actually run transcription.
for seg in segments:
    print(f"[{seg.start:6.2f} -> {seg.end:6.2f}] {seg.text}")
```

With word-level timestamps:

```python
segments, info = model.transcribe(
    "recording.wav",
    word_timestamps=True,
    vad_filter=True,
)
for seg in segments:
    for w in seg.words:
        print(f"{w.start:6.2f} {w.end:6.2f} {w.word!r}  (p={w.probability:.2f})")
```

---

## 6. Rich

### Import

```python
from rich.console import Console
from rich.progress import (
    Progress, BarColumn, TextColumn, TimeRemainingColumn,
    TimeElapsedColumn, MofNCompleteColumn, SpinnerColumn,
    DownloadColumn, TransferSpeedColumn, TaskProgressColumn,
)
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
import rich.box
```

### Console (print, status, rule)

`Console.__init__` — all keyword-only:

| Parameter | Default |
|---|---|
| `color_system` | `"auto"` |
| `force_terminal` | `None` |
| `force_interactive` | `None` |
| `soft_wrap` | `False` |
| `theme` | `None` |
| `stderr` | `False` |
| `file` | `None` |
| `quiet` | `False` |
| `width` | `None` |
| `height` | `None` |
| `style` | `None` |
| `no_color` | `None` |
| `markup` | `True` |
| `emoji` | `True` |
| `highlight` | `True` |
| `record` | `False` |
| `log_time` | `True` |
| `log_path` | `True` |
| `safe_box` | `True` |

`print(*objects, sep=" ", end="\n", style=None, justify=None, overflow=None, no_wrap=None, emoji=None, markup=None, highlight=None, width=None, height=None, crop=True, soft_wrap=None, new_line_start=False)`.

`status(status, *, spinner="dots", spinner_style="status.spinner", speed=1.0, refresh_per_second=12.5) -> Status` — returns a context manager that renders a spinner; starts/stops a refresh thread on enter/exit.

`rule(title="", *, characters="─", style="rule.line", align="center")`.

`log(*objects, sep=" ", end="\n", style=None, justify=None, emoji=None, markup=None, highlight=None, log_locals=False, _stack_offset=1)` — like `print` but prepends a timestamp column on the left and the source file:line on the right.

`capture() -> Capture` — no-arg; use as `with console.capture() as cap:` then `cap.get()`.

**Print to stderr only:** create a dedicated `Console(stderr=True)`.

```python
from rich.console import Console

console = Console()
err_console = Console(stderr=True, style="bold red")  # all output goes to fd 2

console.print("Recording started", style="green")
console.print("[blue underline]https://example.org")        # markup inline
console.rule("[bold red]Transcription")
console.log("processed segment", log_locals=False)

# Spinner context manager — use during recording / transcription
with console.status("Transcribing...", spinner="dots"):
    do_work()

err_console.print("Error: device busy")
```

### Progress bars

`Progress.__init__(*columns, console=None, auto_refresh=True, refresh_per_second=10, speed_estimate_period=30.0, transient=False, redirect_stdout=True, redirect_stderr=True, get_time=None, disable=False, expand=False)`. `*columns` accepts format strings or `ProgressColumn` objects; if omitted, Rich uses a sensible default set.

Built-in column classes in `rich.progress`:

- `BarColumn(complete_style=, finished_style=, pulse_style=)` — the bar.
- `TextColumn(text_format, style=, justify=, table_column=)` — text, with `{task.description}`/`{task.completed}` etc. fields.
- `TaskProgressColumn()` — percentage complete.
- `MofNCompleteColumn()` — `completed/total`.
- `TimeRemainingColumn(compact=, elapsed_when_finished=)` — ETA.
- `TimeElapsedColumn()` — elapsed time.
- `SpinnerColumn(style=, finished_text=)` — animated spinner as a column.
- `DownloadColumn()` — downloaded bytes (assumes steps are bytes).
- `TransferSpeedColumn()` — bytes/sec.
- `FileSizeColumn()` / `TotalFileSizeColumn()`.
- `RenderableColumn(renderable=)` — arbitrary Rich renderable per task.
- `ProgressColumn` — base class for custom columns (override `render(task)`).

Methods:

- `add_task(description, total=None, completed=0, start=True, visible=True, **fields) -> TaskID`.
- `update(task_id, *, total=None, completed=None, advance=None, description=None, visible=None, start=None, **fields)`.
- `advance(task_id, advance=1)` — convenience; `update(task_id, advance=n)` under the hood.
- `start_task(task_id)` / `stop_task(task_id)`.
- `remove_task(task_id)`.
- `refresh()`.
- `finished` — property, `True` when all tasks are 100%.

Indeterminate (`total=None`) shows a pulsing bar; determinate (`total=N`) tracks toward 100%.

```python
import time
from rich.progress import (
    Progress, BarColumn, TextColumn, TimeRemainingColumn,
    TimeElapsedColumn, SpinnerColumn, MofNCompleteColumn,
)

# Custom columns, full width
progress = Progress(
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    MofNCompleteColumn(),
    TimeElapsedColumn(),
    TimeRemainingColumn(),
    expand=True,
)

with progress:
    segments = 137
    # Determinate: total is an int -> percentage tracked
    transcribe = progress.add_task("[cyan]Transcribing", total=segments)
    # Indeterminate: total=None -> pulse until total is set
    align = progress.add_task("[yellow]Aligning...", total=None)

    for i in range(segments):
        time.sleep(0.01)
        progress.advance(transcribe, advance=1)   # per-segment advance

    # Promote the indeterminate task once we know the size
    progress.update(align, total=10, description="[yellow]Aligning")
    for _ in range(10):
        time.sleep(0.01)
        progress.advance(align, advance=1)
```

### Tables

`Table.__init__(*headers, title=None, caption=None, width=None, min_width=None, box=box.HEAVY_HEAD, safe_box=None, padding=(0, 1), collapse_padding=False, pad_edge=True, expand=False, show_header=True, show_footer=False, show_edge=True, show_lines=False, leading=0, style="none", row_styles=None, header_style="table.header", footer_style="table.footer", border_style=None, title_style=None, caption_style=None, title_justify="center", caption_justify="center", highlight=False)`. (Default box is `box.HEAVY_HEAD`.)

`add_column(header="", footer="", *, header_style=None, highlight=None, footer_style=None, style=None, justify="left", vertical="top", overflow="ellipsis", width=None, min_width=None, max_width=None, ratio=None, no_wrap=False)`. `justify` is `"left"` | `"center"` | `"right"`; `overflow` is `"ellipsis"` | `"crop"` | `"fold"`.

`add_row(*renderables, style=None, end_section=False)` — each cell may be any Rich renderable (string with markup, `Text`, another `Table`, etc.).

Box styles in `rich.box`: `ASCII`, `ASCII2`, `ASCII_DOUBLE_HEAD`, `SQUARE`, `SQUARE_DOUBLE_HEAD`, `MINIMAL`, `MINIMAL_DOUBLE_HEAD`, `MINIMAL_HEAVY_HEAD`, `SIMPLE`, `SIMPLE_HEAD`, `SIMPLE_HEAVY`, `HORIZONTALS`, `ROUNDED`, `HEAVY`, `HEAVY_EDGE`, `HEAVY_HEAD`, `DOUBLE`, `DOUBLE_EDGE`, `MARKDOWN`. Pass `box=None` to disable borders. Render with `console.print(table)`.

```python
from rich.console import Console
from rich.table import Table
import rich.box

console = Console()

def render_recording_list(recordings: list[dict]) -> None:
    table = Table(title="Recordings", box=rich.box.ROUNDED, show_lines=False)
    table.add_column("Name",   style="cyan",   no_wrap=True)
    table.add_column("Duration", justify="right", style="magenta")
    table.add_column("Date",   justify="right")
    table.add_column("Status", justify="center", style="green")

    for r in recordings:
        table.add_row(r["name"], r["duration"], r["date"], r["status"])

    console.print(table)

render_recording_list([
    {"name": "standup-2026-07-27.wav", "duration": "32m 04s", "date": "2026-07-27", "status": "ready"},
    {"name": "1on1-alice.wav",         "duration": "48m 11s", "date": "2026-07-26", "status": "transcribing"},
])
```

### Panel (optional)

`Panel(renderable, box=ROUNDED, title=None, title_align="center", subtitle=None, subtitle_align="center", safe_box=None, expand=True, style="none", border_style="none", width=None, height=None, padding=(0, 1), highlight=False)`. `Panel.fit(...)` is a shortcut for `Panel(..., expand=False)`. Render with `console.print(panel)`.

```python
from rich.console import Console
from rich.panel import Panel

console = Console()
# Wrap a status / summary message; expand=False fits content
console.print(
    Panel(
        "[bold green]Recording complete[/]\nSaved: standup-2026-07-27.wav (32m 04s)",
        title="rec status",
        border_style="green",
        expand=False,
    )
)
```

### Gotchas

- **Refresh threads.** Both `Console.status()` and `Progress` start a
  background thread for refresh (Progress default `refresh_per_second=10`,
  status default `12.5`). Always enter them via `with` so the thread is
  stopped; leaving them open leaks threads and corrupts the cursor.
- **Printing inside a `Progress` block breaks the layout.** Raw `print()` /
  `console.print()` interleaves with the live bar. The
  `redirect_stdout=True` / `redirect_stderr=True` defaults on `Progress`
  intercept stdout/stderr and replay them above the bar; to print from within
  the block, use `progress.console.print(...)` (the Console attached to the
  Progress instance) rather than a separate console.
- **`update()` is main-thread-safe.** You call
  `progress.update(task_id, advance=n)` from the main / worker thread; the
  refresh thread only reads. No locking required for typical single-writer
  use.
- **Non-TTY output auto-degrades.** When output is piped (not a TTY), Progress
  detects it and disables the live display / auto-refresh; `add_task`/
  `update` still work but no animation is drawn. Use `force_terminal=True`
  only if you deliberately want ANSI codes in piped output.
- **Nesting `status` and `Progress` is fine with care**, but a raw `print`
  from an inner nested `with` should go to the innermost live's console.
  Prefer a single live layer; if you need spinner + progress together, use a
  `SpinnerColumn` inside `Progress` rather than nesting `console.status`
  inside `with Progress`.
- **`transient=True`** clears the progress display on exit (useful for clean
  logs); default `False` leaves the final bar visible.
- **Indeterminate -> determinate transition:** create the task with
  `total=None` (pulsing bar), then `progress.update(task_id, total=N)` once
  you know the size; the bar switches to tracked mode automatically.
- **`remove_task` vs hiding:** `update(task_id, visible=False)` hides a task
  row while keeping it; `remove_task(task_id)` deletes it entirely.
- **Table width:** columns with `no_wrap=True` plus long cell content may
  overflow; set `max_width=` or `ratio=` on columns, or `overflow="fold"`.
  `expand=True` stretches the table to terminal width; default `False` fits
  content.
- **`add_row` cell count** must match `add_column` count or Rich raises; pass
  `""` for empty cells.
- **Panel default box is `ROUNDED`**, but `Table` default box is
  `box.HEAVY_HEAD` — set `box=` explicitly if you want them to match.

---

## 7. Click

Documentation version: 8.4.x / 8.5.x (stable).

### Import

```python
import click
```

Everything comes from the top-level `click` module: decorators (`command`,
`group`, `option`, `argument`, `pass_context`, `pass_obj`), echo/style
helpers, the `Context` object, parameter types (`Choice`, `Path`, `File`,
`INT`, `STRING`...), and exception classes.

### Group + subcommand pattern

`click.group(*param_decls)` — `name=None, cls=None, **attrs`. Creates a `Group`
(a `Command` subclass that nests other commands). Since 8.1 the decorator can
be applied with or without parentheses.

`Group` constructor defaults that matter:

```
class click.Group(
    name=None,
    commands=None,
    invoke_without_command=False,       # run group callback when no subcommand given
    no_args_is_help=None,               # defaults to opposite of invoke_without_command
    subcommand_metavar=None,
    chain=False,                        # allow multiple subcommands in sequence
    result_callback=None,
    **kwargs,                           # forwarded to Command: help, params, callback, context_settings...
)
```

A group is a command registry via `.commands: MutableMapping[str, Command]`.
Subcommands attach two ways:

1. `Group.command(*args, **kwargs)` decorator — shortcut that builds a
   `Command` from the decorated function and registers it via
   `add_command()`. `name` defaults to the function name lowercased,
   underscores -> dashes, suffixes `_command`/`_cmd`/`_group`/`_grp` stripped
   (since 8.2). Bare `@cli.command` (no parens) works since 8.1.
2. `Group.add_command(cmd, name=None)` for a `Command` built elsewhere.

```python
import click

@click.group()
def cli():
    """Meeting recorder CLI."""
    pass

@cli.command()              # @cli, not @click — registers on the group
def sync():
    """Sync recordings."""
    click.echo("Syncing")
```

`cli` with no args prints help listing `sync`; `cli sync` invokes it.

### Options and arguments

`click.option(*param_decls, cls=None, **attrs)` — `param_decls` are positional
name strings (long/short flags); `**attrs` forwarded to `click.Option`.

Load-bearing kwargs:

| kwarg | type / default | meaning |
| --- | --- | --- |
| `default` | `UNSET` | Default value; may be callable. Type inferred from it if `type` not given. |
| `help` | `None` | Help string for `--help`. |
| `type` | inferred | `ParamType` (`click.Choice`, `click.Path`, `click.File`, `click.INT`) or Python type (`str`/`int`/`float`/`bool`). |
| `required` | `False` | Make the option required. |
| `multiple` | `False` | Accept the option repeatedly; value is a tuple. |
| `nargs` | `1` | Number of values per occurrence (`-1` not valid for options). |
| `is_flag` | auto-detected | Force a boolean flag. |
| `flag_value` | `UNSET` | Value delivered when flag present (auto-sets `is_flag=True`). |
| `count` | `False` | Each occurrence increments an int (`-vvv`). |
| `show_default` | `None` | Show default in help; bool or custom string. |
| `prompt` | `False` | If `True` or str, prompt interactively. |
| `confirmation_prompt` | `False` | Prompt twice (passwords). |
| `hide_input` | `False` | Hide typed input (passwords). |
| `envvar` | `None` | Environment variable name(s) supplying a default. |
| `metavar` | inferred | Value placeholder in help. |
| `expose_value` | `True` | If `False`, value not passed to callback. |
| `hidden` | `False` | Hide option from help. |

Flag forms and name derivation:

- Long `--flag`, short `-f`, both `@click.option('-f', '--flag')`. Short flags
  stack (`-abc`); multi-char short names not supported.
- Destination name taken from the first `--`-prefixed decl, leading dashes
  stripped, `-` -> `_`. Override explicitly:
  `@click.option('--from', 'src', ...)` makes the arg `src`.
- Boolean flag: `@click.option('--shout', is_flag=True)` -> `False` unless
  present. On/off pair auto-detects: `@click.option('--shout/--no-shout', default=False)`.
- Choice: `@click.option('--mode', type=click.Choice(['a','b']), default='a')`.
- Required: `@click.option('--token', required=True)` (or omit `default`).
- Repeated: `@click.option('-m','--message', multiple=True)` -> tuple.

```python
@click.command()
@click.option('--device', '-d', required=True, help='Input device name')
@click.option('--duration', type=int, default=3600, show_default=True,
              help='Max recording length in seconds')
@click.option('--format', type=click.Choice(['wav', 'mp3']), default='wav',
              show_default=True)
@click.option('--verbose/--no-verbose', default=False, help='Verbose output')
def record(device, duration, format, verbose):
    """Record a meeting."""
    click.echo(f"recording {device} for {duration}s as {format}")
```

`click.argument(*param_decls, cls=None, **attrs)` —
`class click.Argument(param_decls, required=None, help=None, **attrs)`.
Differences from option:

- Positional: bare name (`'filename'`), no `--` prefix, supplied in order.
- Required by default (`required` defaults to `True` for arguments, `False`
  for options). Docs caution against forcing `required=True` further.
- No auto help text historically; `help=` is honored on `Argument` only from
  Click 8.5.
- Variadic: `@click.argument('files', nargs=-1)` collects all remaining
  positionals as a tuple (once per command). Fixed multi: `nargs=2` -> tuple.

```python
@click.command()
@click.argument('recording_id')            # required positional
@click.argument('tags', nargs=-1)          # zero or more trailing positionals
def tag(recording_id, tags):
    click.echo(f"tagging {recording_id} with {tags}")
```

### pass_context / sharing state

Each invocation builds a `Context` linked to its parent. Decorate the callback
with `@click.pass_context` to receive the context as the first argument;
`@click.pass_obj` receives `ctx.obj` directly. Because contexts chain to
parents, state set on the group callback is visible to subcommands.

```python
@click.group()
@click.option('--debug/--no-debug', default=False)
@click.pass_context
def cli(ctx, debug):
    ctx.ensure_object(dict)          # create ctx.obj as dict if missing
    ctx.obj['DEBUG'] = debug

@cli.command()
@click.pass_context
def sync(ctx):
    click.echo(f"Debug is {'on' if ctx.obj['DEBUG'] else 'off'}")

@cli.command()
@click.pass_obj
def show(obj):                       # receives ctx.obj directly
    click.echo(obj['DEBUG'])

if __name__ == '__main__':
    cli(obj={})                      # seed ctx.obj so subcommands always see it
```

Related helpers: `click.make_pass_decorator(object_type, ensure=False)` for
typed state; `ctx.exit(code=0)` to exit with a code; `ctx.fail(message)` to
raise `UsageError`; `ctx.invoke(...)` / `ctx.forward(...)` to call another
command.

### Entry point setup in pyproject.toml

The `[project.scripts]` table maps an executable name to
`import.path:function`. The function must be a Click command/group object.

```toml
[project]
name = "mymeetingtool"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = [
    "click>=8.1",
]

[project.scripts]
rec = "mymeetingtool.cli:cli"

[build-system]
requires = ["flit_core<4"]
build-backend = "flit_core.buildapi"
```

Left side (`rec`) is the generated executable; right side is
`<module import path>:<attribute>`. The minimal `cli.py` the entry point
references just needs to expose that group object:

```python
# src/mymeetingtool/cli.py
import click

@click.group()
def cli():
    """Meeting recorder CLI."""
    pass
```

After `pip install -e .`, running `rec --help` works because the installer
generated a `rec` script that calls `mymeetingtool.cli:cli`.

### Minimal full example (rec start / stop / list / status)

`src/mymeetingtool/cli.py`:

```python
import click


@click.group()
@click.version_option()
def cli():
    """Meeting recorder — record, list, and stop meetings."""


@cli.command()
@click.option('--device', '-d', required=True, help='Audio input device name')
@click.option('--duration', type=int, default=3600, show_default=True,
              help='Maximum recording length in seconds')
@click.option('--quality', type=click.Choice(['low', 'high']), default='high',
              show_default=True)
@click.option('--dry-run/--no-dry-run', default=False, help='Do not actually record')
def start(device, duration, quality, dry_run):
    """Start a new recording."""
    click.echo(
        f"start: device={device} duration={duration} quality={quality} dry_run={dry_run}"
    )


@cli.command()
@click.argument('recording_id')          # positional recording id to stop
def stop(recording_id):
    """Stop a running recording by its id."""
    if not recording_id.startswith('rec_'):
        raise click.UsageError("recording_id must start with 'rec_'")
    click.echo(f"stop: stopped {recording_id}")


@cli.command()
@click.option('--limit', type=int, default=50, show_default=True,
              help='Max number of recordings to list')
def list(limit):
    """List recent recordings (takes no positional args)."""
    click.echo(f"list: showing up to {limit} recordings")


@cli.command()
@click.pass_context
def status(ctx):
    """Show recorder status."""
    click.echo(f"status: invoked as {ctx.command_path}")


if __name__ == '__main__':
    cli()
```

Usage: `rec start -d "MacBook Mic" --duration 600`, `rec stop rec_42`,
`rec list --limit 10`, `rec status`. `rec` with no subcommand prints help.

### Gotchas

- Subcommand name derives from the function name (lowercased, `-` for `_`,
  trailing `_command`/`_cmd`/`_group`/`_grp` stripped). Override with `name=`:
  `@cli.command(name='start-recording')`.
- Group callback fires whenever a subcommand fires. To run it even with no
  subcommand, set `invoke_without_command=True` on `@click.group(...)`. By
  default `no_args_is_help` is the opposite, so a bare group invocation shows
  help.
- Options on the group are not auto-forwarded to subcommands; share state via
  `@click.pass_context` + `ctx.obj` (`ctx.ensure_object(dict)` or
  `cli(obj={})` guarantees `ctx.obj` exists), or `@click.pass_obj` /
  `click.make_pass_decorator(MyState, ensure=True)`.
- `@cli.command()` registers on the group — `@cli`, not `@click.command()`.
  Bare `@cli.command` (no parens) also works since 8.1.
- Nested groups: use `@cli.group()` to create a subgroup, then
  `@subgroup.command()` under it. A chain group (`chain=True`) cannot itself
  contain subgroups; only the last chained command may use `nargs=-1`; per
  command, options must precede arguments.
- Use `click.echo(...)` rather than `print()`: it handles encoding on Linux,
  Unicode on Windows consoles, writes bytes safely, strips ANSI when output is
  not a TTY, always flushes, and supports `err=True` to write to stderr and
  `color=` to force ANSI on/off. `click.secho(msg, fg='green')` combines
  `echo` + `style`.
- Errors: raise `click.UsageError(message, ctx=None)` for usage problems,
  `click.ClickException(message)` for any user-facing error (Click prints it
  and exits non-zero), `click.BadParameter(message, ctx=None, param=None, param_hint=None)`
  from a callback/type. `ctx.fail(message)` is shorthand for `UsageError`.
  Exit explicitly with `ctx.exit(code)` (0 = success). `click.Abort` signals
  Ctrl-C-style aborts.
- `default_map` overrides per-parameter defaults — typically loaded from a
  config file. Pass via `context_settings={'default_map': {...}}` or
  `cli(default_map={...})`; it is nested per subcommand:

  ```python
  CONTEXT_SETTINGS = dict(default_map={'runserver': {'port': 5000}})

  @click.group(context_settings=CONTEXT_SETTINGS)
  def cli():
      pass

  @cli.command()
  @click.option('--port', default=8000)
  def runserver(port):
      click.echo(f"Serving on http://127.0.0.1:{port}/")
  ```

- In `@click.command()`/`@click.group()` any decorated `option`/`argument`
  params are appended to `Command.params`; explicit `params=[...]` lists are
  also supported and decorated params are appended after them.
- `Argument.help` is honored only from Click 8.5 onward; on older versions
  arguments get no auto help text, so document them in the command docstring.

---

## Appendix: Source URLs verified

### BlackHole
- https://github.com/ExistentialAudio/BlackHole (README)
- https://github.com/ExistentialAudio/BlackHole/wiki/Multi-Output-Device
- https://github.com/ExistentialAudio/BlackHole/wiki/Getting-Started:-Creating-a-Multi-Output-Device
- https://github.com/ExistentialAudio/BlackHole/discussions/620 (iOS apps on Apple Silicon)
- https://github.com/ExistentialAudio/BlackHole/issues/346 (BlackHole + iOS on M1)

### SwitchAudioSource
- https://github.com/deweller/switchaudio-osx (README)
- https://raw.githubusercontent.com/deweller/switchaudio-osx/master/audio_switch.c (verified flags + JSON format string)
- https://raw.githubusercontent.com/deweller/switchaudio-osx/master/audio_switch.h

### python-sounddevice
- https://python-sounddevice.readthedocs.io/en/latest/api/
- https://python-sounddevice.readthedocs.io/en/latest/api/streams.html (InputStream signature, callback, CallbackFlags)
- https://python-sounddevice.readthedocs.io/en/latest/api/checking-hardware.html (query_devices)
- https://python-sounddevice.readthedocs.io/en/latest/_modules/sounddevice.html (CoreAudioSettings source)
- https://python-sounddevice.readthedocs.io/en/latest/examples.html (rec_unlimited.py)
- https://github.com/spatialaudio/python-sounddevice

### python-soundfile
- https://python-soundfile.readthedocs.io/
- https://python-soundfile.readthedocs.io/en/latest/_modules/soundfile.html (SoundFile source)

### faster-whisper
- https://github.com/SYSTRAN/faster-whisper (README)
- https://pypi.org/project/faster-whisper/
- https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/faster_whisper/transcribe.py (authoritative signatures)
- https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/faster_whisper/vad.py (VadOptions source)
- https://github.com/SYSTRAN/faster-whisper/issues/515 (Apple Metal/MPS support)

### Rich
- https://rich.readthedocs.io/en/latest/console.html
- https://rich.readthedocs.io/en/latest/progress.html
- https://rich.readthedocs.io/en/latest/tables.html
- https://rich.readthedocs.io/en/latest/panel.html
- https://rich.readthedocs.io/en/latest/live.html

### Click
- https://click.palletsprojects.com/en/stable/api/
- https://click.palletsprojects.com/en/stable/commands/
- https://click.palletsprojects.com/en/stable/options/
- https://click.palletsprojects.com/en/stable/arguments/
- https://click.palletsprojects.com/en/stable/complex/
- https://click.palletsprojects.com/en/stable/entry-points/
