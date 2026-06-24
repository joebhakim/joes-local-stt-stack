#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l status_out ($PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictatectl.py" --config "$PERSONAL_STT_CONFIG" status 2>&1)
set -l status_code $status
if test $status_code -ne 0
    printf "%s\n" $status_out >&2
    exit $status_code
end

if string match -q '*recording=True*' -- "$status_out"
    fish "$script_dir/ptt_release_commit.fish"
else
    fish "$script_dir/ptt_press_profile.fish" current
end
