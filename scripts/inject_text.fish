#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

if test (count $argv) -lt 1
    echo "Usage: fish scripts/inject_text.fish \"text to type\" [--backend wtype]"
    exit 2
end

$PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictatectl.py" --config "$PERSONAL_STT_CONFIG" inject-text $argv
