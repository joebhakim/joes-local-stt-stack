#!/usr/bin/env fish
set -l script_dir (dirname (status --current-filename))
source "$script_dir/env.fish"

if not command -q gradle
    echo "error: gradle not found. Install Gradle/Android Studio or add Gradle to PATH." >&2
    exit 1
end

cd "$PERSONAL_STT_ROOT/android"; or exit 1
gradle :app:assembleDebug
