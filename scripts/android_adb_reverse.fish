#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l port "$PERSONAL_STT_ANDROID_BRIDGE_PORT"
if test (count $argv) -gt 0
    set port "$argv[1]"
end

if not command -q adb
    echo "error: adb not found" >&2
    exit 1
end

adb reverse "tcp:$port" "tcp:$port"
echo "Android USB WebSocket URL: ws://127.0.0.1:$port/stt"
