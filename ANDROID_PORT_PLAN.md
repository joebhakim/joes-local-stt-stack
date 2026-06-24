# Android Port Plan

Purpose: move speech recognition to Android while keeping desktop focus,
injection, session state, and visibility in this repo.

## Direction

The Android path should stream recognized text events, not raw audio.
Android owns microphone capture and recognition through `SpeechRecognizer`.
The desktop owns session state, preview/HUD display, and text injection.

```text
Android SpeechRecognizer -> WebSocket JSON events -> desktop bridge -> daemon status/inject-text
```

## Partial Semantics

Partials are first-class mutable drafts. Each partial replaces the current
draft for its session; it is not appended to previous partials.

```json
{"type":"start","session_id":"phone-1","source":"android"}
{"type":"partial","session_id":"phone-1","seq":1,"text":"write a new male"}
{"type":"partial","session_id":"phone-1","seq":2,"text":"write an email"}
{"type":"final","session_id":"phone-1","seq":3,"text":"Write an email."}
```

The desktop should show partials in the floating HUD immediately, but v1 should
only inject final text into the focused application. Live draft typing can be a
later opt-in mode after replacement/backspace behavior is reliable.

## Desktop Contract

The daemon status payload exposes `dictation_display` as the shared display
surface for local Whisper and future Android sessions:

- `session_id`, `source`, `state`, `seq`, `updated_at`
- `committed_text`, `partial_text`, `final_text`
- `partial_is_mutable`, `alignment_lost`, gate and level metadata

The floating HUD consumes this structured status instead of parsing
`state/preview.txt`.

## Bridge

The desktop bridge is `android_bridge.py`. It serves WebSocket JSON on
`0.0.0.0:8765/stt`, requires the PIN in `state/android_bridge_token.txt`, and
forwards events to the daemon's `external_event` command. `adb reverse` is
available through `fish scripts/stt.fish android-reverse`.
