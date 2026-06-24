#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

if not command -q adb
    echo "error: adb not found" >&2
    exit 1
end

set -l apk "$PERSONAL_STT_ROOT/android/app/build/outputs/apk/debug/app-debug.apk"
if not test -f "$apk"
    echo "error: debug APK not found: $apk" >&2
    echo "Run: fish scripts/android_build_debug.fish" >&2
    exit 1
end

adb install -r "$apk"
