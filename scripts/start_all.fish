#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

echo "Starting STT daemon..."
fish "$script_dir/start_daemon_bg.fish"

echo
echo "Starting Android bridge..."
fish "$script_dir/start_android_bridge_bg.fish"

echo
echo "Starting mouse binding..."
bash "$script_dir/start_mouse_dictation_whisper_live.sh"

echo
echo "Starting keyboard binding..."
bash "$script_dir/start_keyboard_dictation.sh"

echo
echo "Starting tray..."
fish "$script_dir/start_tray_bg.fish"
