#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

set -l shortcut "Meta+Ctrl+Alt+F8"
if test (count $argv) -ge 1
    set shortcut "$argv[1]"
end

set -l desktop_dir "$HOME/.local/share/applications"
set -l desktop_file "$desktop_dir/personal-stt-ptt-toggle.desktop"
set -l command "/usr/bin/fish $PERSONAL_STT_ROOT/scripts/ptt_toggle.fish"

mkdir -p "$desktop_dir"

begin
    echo "[Desktop Entry]"
    echo "Type=Application"
    echo "Version=1.0"
    echo "Name=Personal STT PTT Toggle"
    echo "Comment=Toggle local dictation start or commit"
    echo "Exec=$command"
    echo "Icon=audio-input-microphone"
    echo "Terminal=false"
    echo "NoDisplay=true"
    echo "Categories=Utility;"
    echo "X-KDE-Shortcuts=$shortcut"
end > "$desktop_file"

chmod 644 "$desktop_file"

if command -q update-desktop-database
    update-desktop-database "$desktop_dir" >/dev/null 2>/dev/null
end

if command -q kbuildsycoca6
    kbuildsycoca6 --noincremental >/dev/null 2>/dev/null
end

kwriteconfig6 --file "$HOME/.config/kglobalshortcutsrc" \
    --group services --group personal-stt-ptt-toggle.desktop \
    --key _launch "$shortcut,$shortcut,Personal STT PTT Toggle"

if command -q qdbus6
    qdbus6 --literal org.kde.kglobalaccel /kglobalaccel \
        org.kde.KGlobalAccel.getComponent personal-stt-ptt-toggle.desktop >/dev/null 2>/dev/null
end

echo "Installed KDE shortcut for Personal STT toggle"
echo "Shortcut: $shortcut"
echo "Command: $command"
echo "Component: personal-stt-ptt-toggle.desktop"
