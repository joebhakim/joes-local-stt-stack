# Rolling TODOs

Last updated: `2026-06-01T12:00:00-04:00`

## Live Batch

- [x] Created `~/joes-local-stt-stack`.
- [x] Copied faster-whisper daemon, control CLI, profiles, fish helpers, tray
  helper, mouse binder, and smoke fixture.
- [x] Removed old absolute script dependencies on prior source and experiment
  locations.
- [x] Added `pyproject.toml`, `.gitignore`, `AGENTS.md`, `README.md`,
  `WORKING_MEMORY.md`, and `ROLLING_TODOS.md`.
- [x] Initialized this folder as a Git repo.
- [x] Smoke-tested transcription from this folder.
- [x] Started and stopped the standalone daemon unit.
- [x] Migrated active daemon and mouse services from the previous units to
  `personal-stt-*` units.
- [x] Switched status UX from inline marker characters to tray icon state.
- [x] Started active tray service as `personal-stt-tray.service`.
- [ ] Decide whether to create a repo-local `.venv` with `uv`, or continue using
  `~/.venv`.
- [ ] Make an initial commit once the service migration decision is clear.

## Later / Maybe

- [ ] Split `dictate.py` into modules after behavior settles.
- [ ] Add unit tests for config loading, tray activity state, and backend
  selection.
- [ ] Add packaged systemd user service files instead of relying only on
  transient `systemd-run` units.
