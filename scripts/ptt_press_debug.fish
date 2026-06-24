#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
fish "$script_dir/ptt_press_profile.fish" current --debug-streams $argv
