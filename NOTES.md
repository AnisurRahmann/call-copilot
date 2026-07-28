# Engineering Notes — Bugs & Lessons (for interviews)

A candid record of the real, non-obvious problems hit while building **Call Copilot**
(`rec`, a terminal meeting recorder for macOS) and how each was diagnosed and fixed.
Told the way you'd explain it in an interview: what the symptom was, the wrong turns,
how the actual root cause was found, and the takeaway.

---

## 1. The "slow-motion audio" bug — a sample-rate mismatch (the big one)

### Symptom
User recorded real audio (a video playing). The transcript was nonsense:
repeating phrases like *"I am the only one who can do this"* and Whisper's
hallucination signature *"please click on the link in the description below."*

### The wrong turn: "throw a bigger model at it"
My first instinct was an ML-fix reflex: the transcript is garbage, so the model must be
too weak — let's bump from `base` to `small` or `medium`. This is a classic trap: when
output looks wrong, assume the model is the problem. I was about to spend ~10 minutes
downloading a ~480 MB–1.5 GB model that **would not have helped at all** (a bigger model
fed the same mislabeled, 3×-slowed audio would hallucinate the same garbage).

### What broke me out of it
I stopped staring at the transcript and actually **listened to a sample of the captured
audio**. It sounded like **"slow motion … if you slow down any sound it hears like
that."** That single observation — from the *input*, not the output — was the textbook
signature of a **sample-rate mismatch**: audio captured at one rate, played/transcribed
at another → wrong speed + wrong pitch. The bug was never in the model; it was in the
data feeding it. The bigger-model detour would have wasted time and "confirmed" a wrong
hypothesis (it would still produce garbage, leading me further from the truth).

### Why listening was the move
The pipeline is tap → WAV → resample → VAD → Whisper → markdown. A failure at the end
(empty/garbage transcript) can originate at *any* stage. Inspecting only the *final
output* keeps you guessing about which stage is broken. Listening to the raw capture
collapsed the search space instantly: the audio was wrong *before* Whisper saw it, so
the model was exonerated and the hunt focused upstream.

### Root cause (verified empirically)
The recorder used the `audiotap` library and passed `sample_rate=16000`. I assumed
that meant "deliver audio at 16 kHz." I measured what audiotap *actually* delivered:

```
requested=16000 → delivered=47282 Hz   (NO MATCH)
requested=22050 → delivered=47178 Hz   (NO MATCH)
requested=44100 → delivered=47126 Hz   (NO MATCH)
requested=48000 → delivered=47301 Hz   (MATCH — only because it equals native)
```

**audiotap ignores `sample_rate` entirely** and always captures at the device's native
rate (~47 kHz on this Mac). The recorder then stamped the WAV file as **16000 Hz**, so:
- playback read "16k samples/sec" → a 47 kHz stream played **~3× too slow** (the "slow mo");
- Whisper read the same mislabeled file, "heard" slowed-down gibberish, and hallucinated.

### The fix
1. **Detect the true rate at runtime.** Count frames delivered over a ~1s window at tap
   startup; snap to the nearest common rate (48000). Write the WAV at *that* rate so
   playback is correct speed.
2. **Resample before transcription.** Load the WAV, downmix to mono, linear-resample to
   16 kHz, and feed that to Whisper. Speech bandwidth is <8 kHz so linear interpolation
   is sufficient (avoids a scipy dependency).
3. **Don't lose the first second.** The rate-measurement step drains the queue — I made
   it return the drained chunks so they get written to the WAV too.

### Proof
Re-transcribed the same broken recording through the new path: garbage
(*"click on the link…"*) became coherent real speech (*"If you have any questions,
please let us know in the comment section below…"*, 57 words).

### Takeaways for an interviewer
- **Inspect the input, not just the output.** My first reflex was to upgrade the model —
  a classic ML-fix trap that would have wasted time and sent me further from the truth.
  Listening to a sample of the *captured audio* (the input) collapsed the search space
  instantly: it was a data bug, not a model bug. When output is wrong, look at what's
  feeding the system before blaming the model.
- **Symptoms can be deceptive.** A "bad transcript" can be a *data pipeline* bug, not an
  ML bug. Always inspect the actual bytes/waveform before theorizing.
- **Don't trust a library's parameter names.** Verify behavior empirically — I measured
  the real frame rate instead of assuming `sample_rate=` did what it said.
- **The user is a source of truth I don't have.** I literally could not know the audio
  sounded slow without listening to a sample of it. `afplay` on a 47 kHz stream labeled
  16 kHz was the proof.
- **Reproducible A/B tests.** I proved the fix on the *existing* recording — no need to
  re-record — by resampling offline and re-transcribing.

---

## 2. The empty-transcript bug — VAD discarding real speech

### Symptom
After fixing the capture, a recording with clear audio still produced **0 words**.
And the silence detector didn't fire — the recording genuinely wasn't silent.

### Root cause
faster-whisper's **Silero VAD (voice activity detection)** pre-filter was rejecting
100% of the audio as "non-speech." The logs were the smoking gun:
```
VAD filter removed 01:50.880 of audio     ← removed 100%
VAD filter kept the following segments:    ← kept NOTHING
transcription complete: 0 segments
```
Silero VAD is tuned for **close-mic speech**. System-audio capture (speakers/headphones
routed through a Core Audio tap) has a different frequency/level character, so the VAD
threw it all away *before* Whisper ever saw it.

### The fix
- **Turned VAD off by default.** Whisper's own `no_speech_threshold` handles silence
  adequately without the risky pre-filter.
- Kept `--vad` as an opt-in flag for the close-mic case where it genuinely helps.
- I tested tuning the VAD threshold down first — even at 0.2 it only recovered 13 of 48
  words and admitted hallucinations. Off was the robust choice.

### Proof
A/B test on the same real recording: `vad_filter=True` → 0 words; `vad_filter=False` →
48 words.

### Takeaway
**Defaults matter.** A "helpful" optimization (VAD) was silently destroying user data.
When a pre-filter can discard input, its default should be conservative, and there must
be a way to bypass it.

---

## 3. "Silent recording" misdiagnosis — the importance of ground truth

### What happened
Early recordings came back empty (0 words). I initially blamed the capture and rewrote
the entire audio stack. But I jumped to conclusions before verifying the audio content.

### What I should have done (and now do)
**Inspect the actual waveform first.** When I finally ran `numpy.abs(data).max()`, the
truth split three ways:
- Some recordings: `peak=0.0` → genuinely silent (nothing was playing). Capture worked fine.
- Other recordings: `peak=0.49` → loud, real audio → the bug was downstream in transcription.

### Fix built from this
Added `audio_check.analyze_wav()` that streams the WAV and reports peak/RMS. `rec stop`
now warns loudly *immediately* if a recording is silent — before wasting a Whisper run —
and logs the levels to `session.json` for the diagnose bundle.

### Takeaway
**Establish ground truth before theorizing.** "Empty transcript" has at least three
distinct causes (no audio / wrong rate / VAD); only inspecting the samples tells you which.
The debugging tool (`rec diagnose`) exists precisely so this is one command.

---

## 4. The architectural decision: why we abandoned the BlackHole driver

### The problem
The original design used **BlackHole** (a virtual audio driver) + a manually-created
**Multi-Output Device** in Audio MIDI Setup to tap system audio. It was fragile:
- Silent recordings when the Multi-Output Device was mis-configured (BlackHole not
  actually a member / not receiving the stream). I proved this with a live test — a loud
  tone played through the Multi-Output produced `peak=0` on BlackHole.
- Required manual GUI setup the CLI couldn't automate (macOS won't let CLI tools create
  aggregate devices).
- Switched the user's system output device and had to restore it (more failure modes).

### The research that ruled out the "obvious" fix
I first considered rewriting the tap in pure Python. I traced the exact Core Audio calls
needed: `AudioHardwareCreateProcessTap(CATapDescription, ...)`. The blockers were hard:
- `CATapDescription` is an **Objective-C class**, not a struct — can't be built with `ctypes` alone.
- I verified against PyObjC's actual changelog that it does **not** expose this class (an
  LLM-synthesized search result *claimed* it did — that was a hallucination; I checked the source).
- The audio callback runs on Core Audio's **real-time thread** — Python (GIL, GC) can't
  safely run there. That's why any working implementation is Swift/C/ObjC, not Python.

### The solution
Switched to **`audiotap`** (a pip-installable package bundling a minimal C dylib that wraps
the Core Audio taps API, macOS 14.2+). Same mechanism as the Swift `audiotee` tool, but
packaged as a Python library — no driver to install, no Multi-Output Device, no device
switching, no silent-recording failure mode.

### Takeaway
**When a foundation is fragile, replace it — but verify the replacement is feasible before
committing.** I spent an hour confirming pure-Python was impossible *before* writing code,
which prevented a doomed rewrite. And I distinguished a real library (checked the actual
changelog) from an LLM hallucination about one.

---

## 5. Cross-cutting lesson: debugging a multi-stage audio pipeline

The recorder is a pipeline: **tap → queue → WAV → resample → VAD → Whisper → markdown.**
A failure at the end (empty transcript) could originate at *any* stage. The lesson:

1. **Measure at every boundary.** Peak/RMS on the WAV (is the capture OK?), the labeled
   vs. true sample rate (is the file correct?), VAD kept/removed seconds (is the pre-filter
   discarding data?), segment count (did Whisper produce anything?).
2. **Logs that stamp context.** Every log line carries `[session_id|command]`, so a single
   `rec diagnose <session>` bundles everything an AI agent (or future me) needs.
3. **The user's ears are a sensor I don't have.** When data is ambiguous, ask them to listen.

---

## Quick "hardest bug" answer (30-second version)

> "The hardest bug was a sample-rate mismatch. Transcripts came back as nonsense, and my
> first instinct was the classic ML-fix trap — upgrade Whisper from `base` to a bigger
> model. I was about to download 1.5 GB that wouldn't have helped. What broke me out of it
> was listening to a sample of the *captured audio*: it sounded like slow motion. That's the
> signature of a rate mismatch. Our audio library silently ignored the sample rate we
> requested and captured at the device's native 48 kHz, but we wrote the file as 16 kHz — so
> recordings played 3× too slow and Whisper hallucinated from the garbage. The lesson: when
> output is wrong, inspect the input before blaming the model. I measured the real frame
> rate, fixed the recorder to detect the true rate at runtime and resample to 16 kHz before
> transcription, and proved it by re-transcribing an existing broken recording — it went
> from hallucinated loops to a clean transcript."
