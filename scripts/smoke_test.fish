#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

$PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictate.py" smoke --config "$PERSONAL_STT_CONFIG" --audio "$PERSONAL_STT_ROOT/jfk.wav"
