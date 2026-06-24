#!/usr/bin/env fish

set -l script_dir (dirname (status --current-filename))
set -gx PERSONAL_STT_ROOT (realpath "$script_dir/..")
set -gx PERSONAL_STT_CONFIG "$PERSONAL_STT_ROOT/config.toml"

if not set -q PERSONAL_STT_DAEMON_UNIT
    set -gx PERSONAL_STT_DAEMON_UNIT personal-stt-daemon
end

if not set -q PERSONAL_STT_MOUSE_UNIT
    set -gx PERSONAL_STT_MOUSE_UNIT personal-stt-mouse-dictation
end

if not set -q PERSONAL_STT_KEYBOARD_UNIT
    set -gx PERSONAL_STT_KEYBOARD_UNIT personal-stt-keyboard-dictation
end

if not set -q PERSONAL_STT_TRAY_UNIT
    set -gx PERSONAL_STT_TRAY_UNIT personal-stt-tray
end

if not set -q PERSONAL_STT_ANDROID_BRIDGE_UNIT
    set -gx PERSONAL_STT_ANDROID_BRIDGE_UNIT personal-stt-android-bridge
end

if not set -q PERSONAL_STT_ANDROID_BRIDGE_PORT
    set -gx PERSONAL_STT_ANDROID_BRIDGE_PORT 8765
end

set -l venv_candidates
if set -q PERSONAL_STT_VENV
    set -a venv_candidates "$PERSONAL_STT_VENV"
end
set -a venv_candidates "$PERSONAL_STT_ROOT/.venv" "$PERSONAL_STT_ROOT/.venv-stt"
if set -q HOME
    set -a venv_candidates "$HOME/.venv"
end

for venv in $venv_candidates
    if test -f "$venv/bin/activate.fish"
        source "$venv/bin/activate.fish"
        set -gx PERSONAL_STT_PYTHON "$venv/bin/python"
        break
    end
end

if not set -q PERSONAL_STT_PYTHON
    if command -q python3
        set -gx PERSONAL_STT_PYTHON (command -s python3)
    else if command -q python
        set -gx PERSONAL_STT_PYTHON (command -s python)
    else
        echo "error: Python interpreter not found"
        exit 1
    end
end

set -l site_packages ($PERSONAL_STT_PYTHON -c 'import site; print(site.getsitepackages()[0])' 2>/dev/null)
if test -n "$site_packages"
    set -l cublas_dir "$site_packages/nvidia/cublas/lib"
    set -l cudnn_dir "$site_packages/nvidia/cudnn/lib"
    if test -d "$cublas_dir"; and test -d "$cudnn_dir"
        if set -q LD_LIBRARY_PATH
            set -gx LD_LIBRARY_PATH "$cublas_dir:$cudnn_dir:$LD_LIBRARY_PATH"
        else
            set -gx LD_LIBRARY_PATH "$cublas_dir:$cudnn_dir"
        end
    end
end

mkdir -p "$PERSONAL_STT_ROOT/state" "$PERSONAL_STT_ROOT/logs"
