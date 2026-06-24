#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))

set -l action "toggle"
if test (count $argv) -gt 0
    set action (string lower -- "$argv[1]")
    set -e argv[1]
end

switch "$action"
    case toggle
        fish "$script_dir/ptt_toggle.fish" $argv
    case press down start
        fish "$script_dir/ptt_press.fish" $argv
    case press-kitty kitty
        fish "$script_dir/ptt_press.fish" $argv
    case press-system press-rest system rest
        fish "$script_dir/ptt_press.fish" $argv
    case release up commit stop
        fish "$script_dir/ptt_release_commit.fish"
    case cancel abort
        fish "$script_dir/ptt_release_cancel.fish"
    case '*'
        echo "Usage:"
        echo "  fish scripts/ptt_switch.fish toggle [--input-target ffine]"
        echo "  fish scripts/ptt_switch.fish press [--input-target ffine]"
        echo "  fish scripts/ptt_switch.fish release"
        echo "  fish scripts/ptt_switch.fish cancel"
        exit 2
end
