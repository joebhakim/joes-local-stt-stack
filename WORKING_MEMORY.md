# Working Memory

Last updated: `2026-06-01T00:00:00-04:00`

## Current Short State

- This repo is the standalone copy of the personal faster-whisper dictation
  stack.
- It is rooted at `~/joes-local-stt-stack`.
- The original live services under the previous units are still the active daily
  services; the standalone test daemon was started and stopped successfully as
  `personal-stt-daemon.service`.
- Scripts resolve paths from this repo via `scripts/env.fish`.
- Runtime files are repo-local under `state/` and `logs/`, and are gitignored.
- Python lookup prefers repo-local `.venv`, then repo-local `.venv-stt`, then
  `~/.venv`.
- Smoke test passed with `large-v3` on CUDA/float16 using `jfk.wav`.
- UX is now single-route dictation with tray status:
  `Shift+Button9` records into the focused application and `Shift+Button8`
  commits. Inline text-field markers are disabled; the tray icon shows
  recording/finalizing/committed/no-voice/error state.
- Dedicated X11 extra-key toggles are available through
  `scripts/x11_keyboard_dictation.py`: `XF86HomePage` starts `default`,
  `XF86Mail` commits, and `XF86PowerOff`/`XF86PowerDown` start debug recording.
  The normal navigation `Home` key is not bound. KDE's `org_kde_powerdevil`
  `PowerOff` and `PowerDown` shortcuts are disabled, but the physical power key
  still needs `systemd-logind` `HandlePowerKey=ignore` to avoid sleep on
  keyboards that bypass KDE.
- The experimental superfast mouse route is paused. Active daily dictation is
  back to the stable `default` route: `Shift+Button9` starts and `Shift+Button8`
  commits.
- Subtle sound cues are enabled by default through `[sound]` in `config.toml`
  using `paplay` and the local KDE Ocean sound set. `voice_ready` plays when
  the recording crosses from rejecting-silence to accepting-voice.
- Silence protection is now stricter in live mode: the old 1.0% RMS threshold
  is still present, but live decode/final commit require sustained signal before
  any text is injected.
- The tray icon now makes that gate visible while recording with a level meter
  and accepting-vs-rejecting indicator.
- `ydotool type` is currently run with `--key-delay 4 --key-hold 4` to make
  synthetic typing much faster than the upstream defaults.
- Service management is consolidated through `scripts/stt.fish`, which wraps
  start/stop/restart/status/logs for the daemon, Android bridge, mouse
  listener, keyboard listener, and tray.
- Live stream debugging is available manually through
  `scripts/ptt_press_debug.fish`. It suppresses live insertion, simulates the
  live accept path, and writes a fixed-layout ASCII board to `state/preview.txt`;
  final commit still injects the full final text.
- The daemon now exposes a structured `dictation_display` status object for
  first-class accepted/partial/final state. The tray process uses it for a
  draggable floating HUD with start/commit/cancel/live controls.
- The floating HUD now has a compact/detailed toggle. Detailed mode shows
  scrollable field tables for session/audio state plus parsed sections from
  `state/preview.txt`, reusing the existing debug watch-board diagnostics; the
  `Tray` button hides the floating HUD while leaving the system tray indicator
  running.
- The daemon exposes `last_result` in status so the HUD can show the last
  no-voice, commit, divergence skip, injection failure, cancellation, or
  Android/external outcome directly instead of requiring journal inspection.
- Android STT now has a desktop WebSocket bridge plan and scaffold:
  `android_bridge.py`, service scripts, token/adb helpers, and a native Kotlin
  app under `android/`. Phone partials are mutable replacement drafts and final
  results are the injection authority.
- Offline live-strategy regression checks are available through
  `scripts/test_live_strategy.fish`. The default synthetic suite is fast; audio
  mode uses faster-whisper rolling-window probes without injecting text.

## Active Decisions

- Keep this repo self-contained; do not point scripts back to old source or
  experiment locations.
- Keep dictated text fields clean. Do not use inline Unicode marker characters
  for status; use tray state instead.
- Do not commit runtime artifacts, virtualenvs, model caches, logs, state files,
  or `__pycache__/`.

## Pointers

- Usage docs: `README.md`
- Runtime config: `config.toml`
- Default profile: `profiles/default.toml`
- Superfast profile: `profiles/superfast.toml`
- Daemon source: `dictate.py`
- Control CLI: `dictatectl.py`
- Live strategy harness: `scripts/live_strategy_harness.py`

## 2026-06-02 live commit divergence fix

User observed repeated cases where the HUD/accepted/final text contained the correct transcript but the target app stopped receiving text mid-utterance. Logs showed `Live/final divergence ... skipping final injection to avoid duplicate text` in the old `prefix` live strategy.

Changes made:
- `profiles/default.toml` now sets `live_strategy = "rolling_append"`.
- `profiles/obsidian.toml` now sets `live_strategy = "rolling_append"`.
- `dictate.py` finalization fallback for the legacy prefix path now tries `_remove_live_injected_text()` and injects the full final transcript on divergence instead of silently skipping. If removal fails, it still skips to avoid duplicating/deleting unknown user text.

Rationale: the daemon should only erase text it believes it injected during the current live session, but if final transcription disagrees with live partials, replacing our own live text is safer than leaving the target app missing the tail.

## 2026-06-02 manual final/live divergence simulator

Added a manual simulator for the bug where live text is injected, Whisper later corrects/diverges, and finalization needs to delete the live text and replace it with the final transcript.

CLI shape:

```bash
python3 dictatectl.py simulate-divergence \
  --live "The last two ish rounds rounds" \
  --final "The last two-ish rounds did have a bug where it sort of just stopped inputting text." \
  --confirm-delete
```

Safety:
- Requires `--confirm-delete`.
- Refuses to run while a local or external recording session is active.
- Uses normal injection backends and `_remove_live_injected_text()` so it exercises the same delete/backspace mechanism as the real finalization fallback.
- Use only in a scratch text field because it intentionally types, backspaces, and retypes.

Expected action for divergence cases: `replace_live_with_final`.
Expected action for clean prefix cases: `append_remainder`.

## 2026-06-02 profile rename and HUD selector

Renamed the stable daily profile from `codex` to `default`:
- `profiles/codex.toml` -> `profiles/default.toml`
- `config.toml` default profile is now `default`
- Home/mouse/debug hotkey scripts now start the currently selected profile
- `scripts/profile_codex.fish` -> `scripts/profile_default.fish`

HUD compact mode now shows the active profile and includes a dropdown profile selector. Normal UI exposes `default` and `obsidian`; `superfast` remains an experimental profile file but is hidden from the compact selector unless it becomes active.

After changing the transient mouse/keyboard service commands, recreated both user units from the updated start scripts rather than only restarting the old loaded units.

Follow-up fix: Home/mouse/toggle/debug start paths now call `ptt_press_profile.fish current`, which starts recording without switching profiles. This prevents a HUD-selected profile, for example `mutable`, from snapping back to `default` when recording starts.

## 2026-06-02 mutable draft profile

Added `profiles/mutable.toml` as the first explicit fast-delete / draft-replace profile:
- `live_strategy = "draft_replace"`
- `chunk_ms = 180`
- `window_seconds = 8.0`
- `min_window_seconds = 0.7`
- `stable_rounds = 1`

Mechanics: the daemon injects the current draft into the focused text field, remembers the exact injected payload, then rapidly backspaces that payload and injects the next draft when recognition changes. Finalization removes the live draft and injects the final transcript.

Caveat: this is intentionally an experimental profile for short/medium bursts. For robust long-form dictation, implement a dedicated `mutable_tail` strategy that appends stable text normally and only backspaces/replaces the unstable tail.

## 2026-06-02 mutable tail replacement

Testing showed the first `mutable` profile (`draft_replace`) was wrong for long utterances: it deleted/retyped the whole rolling-window transcript, so after the 8-12s window rolled it flashed only the recent window and could remove earlier good text.

Changed `mutable` to `live_strategy = "mutable_tail"` and added that strategy in `dictate.py`.

Intended mechanics:
- Align current rolling transcript against the already committed tail, like `rolling_append`.
- Append accepted/stable tokens normally.
- Keep only the last ~2 candidate tokens as mutable draft text in the real field.
- On new partials, backspace only the mutable draft and replace it.
- Never delete already accepted committed text during live partial processing; final commit may still remove/replace our full live-injected text if final/live divergence requires repair.

## 2026-06-02 default live acceptance fix

A default-profile long utterance produced a duplicated-looking text field, but `state/last_commit.txt` and `last_result.text` contained a clean final transcript. The session status showed `committed_tokens=0` and `final_action=full_final`, so default mode had not accepted any live tokens during a ~30s recording and injected the whole final transcript at commit.

Cause: `rolling_append` required the entire candidate suffix to be identical for `stable_rounds`, so growing transcripts often never became accepted. Changed `rolling_append` to accept the stable common prefix between the previous pending suffix and the current suffix. This should allow live accepted chunks to advance while speech continues.

Also fixed `mutable_tail` finalization: it now removes only the mutable tail and appends the final remainder after already accepted stable text. It no longer injects the whole final transcript after stable accepted text, which would duplicate the prefix.
