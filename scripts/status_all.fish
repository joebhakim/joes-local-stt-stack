#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

function print_unit --argument-names label unit
    if not command -q systemctl
        printf "%-8s %-40s %s\n" "$label" "$unit.service" "systemctl unavailable"
        return
    end

    set -l active (systemctl --user is-active "$unit.service" 2>/dev/null)
    if test $status -ne 0
        set active inactive
    end

    set -l substate (systemctl --user show "$unit.service" --property=SubState --value 2>/dev/null)
    if test -z "$substate"
        set substate unknown
    end

    set -l pid (systemctl --user show "$unit.service" --property=MainPID --value 2>/dev/null)
    if test -z "$pid"; or test "$pid" = 0
        set pid -
    end

    printf "%-8s %-40s %-8s %-10s pid=%s\n" "$label" "$unit.service" "$active" "$substate" "$pid"
end

echo "Services"
print_unit daemon "$PERSONAL_STT_DAEMON_UNIT"
print_unit bridge "$PERSONAL_STT_ANDROID_BRIDGE_UNIT"
print_unit mouse "$PERSONAL_STT_MOUSE_UNIT"
print_unit keyboard "$PERSONAL_STT_KEYBOARD_UNIT"
print_unit tray "$PERSONAL_STT_TRAY_UNIT"

echo
echo "Daemon"
$PERSONAL_STT_PYTHON "$PERSONAL_STT_ROOT/dictatectl.py" --config "$PERSONAL_STT_CONFIG" status
