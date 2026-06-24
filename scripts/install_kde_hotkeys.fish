#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l home_dir ""
if set -q HOME; and test -n "$HOME"
    set home_dir "$HOME"
else
    set -l user_name (id -un 2>/dev/null)
    if test -n "$user_name"; and command -q getent
        set home_dir (string split -f6 : (getent passwd "$user_name" 2>/dev/null))
    end
end

if test -z "$home_dir"; or not test -d "$home_dir"
    echo "error: could not resolve user home directory"
    exit 1
end

set -g desktop_dir "$home_dir/.local/share/applications"
set -g shortcuts_file "$home_dir/.config/kglobalshortcutsrc"

if not command -q kwriteconfig6
    echo "error: kwriteconfig6 not found (KDE tools required)"
    exit 1
end

mkdir -p "$desktop_dir"

function install_action --argument-names id name comment command shortcut
    set -l desktop_file "$desktop_dir/$id.desktop"

    if not begin
        echo "[Desktop Entry]"
        echo "Type=Application"
        echo "Version=1.0"
        echo "Name=$name"
        echo "Comment=$comment"
        echo "Exec=$command"
        echo "Icon=audio-input-microphone"
        echo "Terminal=false"
        echo "NoDisplay=true"
        echo "Categories=Utility;"
        echo "X-KDE-Shortcuts=$shortcut"
    end > "$desktop_file"
        echo "error: failed writing $desktop_file"
        return 1
    end

    if not chmod 644 "$desktop_file"
        echo "error: failed chmod on $desktop_file"
        return 1
    end

    if not kwriteconfig6 --file "$shortcuts_file" \
        --group services --group "$id.desktop" \
        --key _launch "$shortcut,$shortcut,$name"
        echo "error: failed writing shortcut config for $id.desktop"
        return 1
    end

    if command -q qdbus6
        qdbus6 --literal org.kde.kglobalaccel /kglobalaccel \
            org.kde.KGlobalAccel.getComponent "$id.desktop" >/dev/null 2>/dev/null
    end

    echo "installed: $name"
    echo "  shortcut: $shortcut"
    echo "  command : $command"
end

set -l root "$PERSONAL_STT_ROOT/scripts"
set -l record_shortcut "Meta+Ctrl+Shift+R"
set -l commit_shortcut "Meta+Ctrl+Shift+C"
set -l discard_shortcut "Meta+Ctrl+Shift+X"
set -l live_mode_shortcut "Meta+Ctrl+Shift+L"

function disable_action --argument-names id label
    kwriteconfig6 --file "$shortcuts_file" \
        --group services --group "$id.desktop" \
        --key _launch "none,none,$label" >/dev/null 2>/dev/null
end

install_action \
    "personal-stt-ptt-press" \
    "Personal STT Record" \
    "Start dictation recording" \
    "/usr/bin/fish $root/ptt_press.fish" \
    "$record_shortcut"; or exit 1

install_action \
    "personal-stt-ptt-release-commit" \
    "Personal STT Commit" \
    "Commit current dictation recording" \
    "/usr/bin/fish $root/ptt_release_commit.fish" \
    "$commit_shortcut"; or exit 1

install_action \
    "personal-stt-ptt-release-cancel" \
    "Personal STT Discard" \
    "Discard current dictation recording" \
    "/usr/bin/fish $root/ptt_release_cancel.fish" \
    "$discard_shortcut"; or exit 1

install_action \
    "personal-stt-toggle-live" \
    "Personal STT Live Mode" \
    "Toggle dictation mode between ptt and live" \
    "/usr/bin/fish $root/toggle_live.fish" \
    "$live_mode_shortcut"; or exit 1

# Disable old optional actions so they don't keep competing for shortcuts.
disable_action "stt-ptt-toggle" "STT Toggle"
disable_action "stt-safe-test-clipboard" "STT Safe Clipboard Test"
disable_action "personal-stt-ptt-press-kitty" "Personal STT Record (Kitty)"

if command -q update-desktop-database
    update-desktop-database "$desktop_dir" >/dev/null 2>/dev/null
end

if command -q kbuildsycoca6
    kbuildsycoca6 --noincremental >/dev/null 2>/dev/null
end

if command -q qdbus6
    qdbus6 org.kde.KWin /KWin reconfigure >/dev/null 2>/dev/null
end

echo ""
echo "KDE STT hotkeys installed."
echo "Default map:"
echo "  $record_shortcut  record/start"
echo "  $commit_shortcut  commit"
echo "  $discard_shortcut  discard"
echo "  $live_mode_shortcut  toggle live mode"
echo ""
echo "Note: KDE global shortcuts are key-press actions, not key-release hooks."
