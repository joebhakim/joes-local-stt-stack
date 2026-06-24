# AGENTS.md

Purpose: standalone personal speech-to-text stack.

## Canonical Docs

- Keep `README.md` current for durable usage and setup.
- Keep `WORKING_MEMORY.md` for current implementation state if active agent
  work becomes nontrivial.
- Keep `ROLLING_TODOS.md` as a casual live queue if multiple follow-ups build
  up.

## Runtime Rules

- Use `uv` for Python dependency management.
- Prefer a repo-local `.venv` when making this portable.
- A shared `~/.venv` may also be used if it already has the needed packages.
- Ask before installing packages.

## Local Caution

- Do not commit `state/`, `logs/`, `__pycache__/`, model caches, virtualenvs, or
  local daemon pid/status files.
- Keep scripts self-contained: resolve paths from the repo root, not from an old
  checkout location.
