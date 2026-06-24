#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

echo "Stopping mouse binding..."
bash "$script_dir/stop_mouse_dictation.sh"

echo
echo "Stopping keyboard binding..."
bash "$script_dir/stop_keyboard_dictation.sh"

echo
echo "Stopping tray..."
fish "$script_dir/stop_tray.fish"

echo
echo "Stopping Android bridge..."
fish "$script_dir/stop_android_bridge.fish"

echo
echo "Stopping STT daemon..."
fish "$script_dir/stop_daemon.fish"
