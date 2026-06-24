# Personal STT Android App

This app streams Android `SpeechRecognizer` events to the desktop bridge.
Partials are mutable replacement drafts. Final results are injected by the
desktop.

## Desktop Setup

```fish
cd ~/joes-local-stt-stack
fish scripts/stt.fish start
fish scripts/stt.fish android-pin
fish scripts/stt.fish android-reverse
```

For USB mode, use this URL in the app:

```text
ws://127.0.0.1:8765/stt
```

For Wi-Fi mode, use:

```text
ws://<desktop-ip>:8765/stt
```

The PIN is always required.

## Build

This repo contains the Android Gradle project, but it does not vendor the
Android SDK or Gradle wrapper.

```fish
fish scripts/android_build_debug.fish
fish scripts/android_install_debug.fish
```

If Gradle or the Android SDK is missing, install them first or open the
`android/` project in Android Studio.
