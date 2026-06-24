#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

$PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/scripts/live_strategy_harness.py" $argv
