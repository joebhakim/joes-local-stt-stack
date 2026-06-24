#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l pid_file "$PERSONAL_STT_ROOT/state/tray.pid"
if not test -f "$pid_file"
    echo "tray running=False"
    exit 1
end

set -l pid (string trim -- (cat "$pid_file"))
if test -z "$pid"
    echo "tray running=False"
    exit 1
end

kill -0 "$pid" >/dev/null 2>/dev/null
if test $status -eq 0
    echo "tray running=True pid=$pid"
    exit 0
end

echo "tray running=False"
exit 1
