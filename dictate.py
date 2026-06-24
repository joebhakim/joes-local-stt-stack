#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import deque
import json
import logging
import os
import shutil
import signal
import site
import socket
import socketserver
import subprocess
import sys
import textwrap
import threading
import time
import traceback
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


def _bootstrap_cuda_library_path() -> None:
    # Set LD_LIBRARY_PATH early so CUDA libs are discoverable before loading backend modules.
    candidates: list[str] = []
    for site_dir in site.getsitepackages():
        for rel in ("nvidia/cublas/lib", "nvidia/cudnn/lib"):
            path = Path(site_dir) / rel
            if path.exists():
                candidates.append(str(path))

    if not candidates:
        return

    current = os.environ.get("LD_LIBRARY_PATH", "")
    merged = ":".join(candidates + ([current] if current else []))
    os.environ["LD_LIBRARY_PATH"] = merged


_bootstrap_cuda_library_path()

from faster_whisper import WhisperModel


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"
UNSET = object()
LIVE_MARKERS = {
    "recording": "◉",
    "decoding": "◇",
    "active": "▶",
    "finalizing": "◐",
    "committed": "✔",
    "cancelled": "✖",
    "error": "⚠",
}
LIVE_VOLUME_BLOCKS = "▁▂▃▄▅▆▇█"
LIVE_INPUT_MARKER = LIVE_MARKERS["recording"]
LIVE_MARKER_FLASH_SECONDS = 0.2
AUDIO_LEVEL_THRESHOLD_PERCENT = 1.0
AUDIO_LEVEL_STRONG_THRESHOLD_PERCENT = 2.0
AUDIO_LEVEL_READY_MIN_CHUNKS = 8
AUDIO_LEVEL_READY_MIN_STRONG_CHUNKS = 3
AUDIO_LEVEL_DISPLAY_MAX_PERCENT = 12.0
AUDIO_LEVEL_GRACE_SECONDS = 1.2
LIVE_LEVEL_MARKER_UPDATE_SECONDS = 0.12
PREVIEW_TEXT_WIDTH = 96
PREVIEW_COMMITTED_ROWS = 3
PREVIEW_TAIL_ROWS = 3
DEBUG_SNAPSHOT_ROWS = 8
DEBUG_EVENT_ROWS = 12
ROLLING_APPEND_MIN_OVERLAP_TOKENS = 3
ROLLING_APPEND_COMMITTED_TAIL_TOKENS = 32
YDOTOOL_BACKSPACE_KEY_DELAY_MS = 1
XDOTOOL_BACKSPACE_DELAY_MS = 1


@dataclass
class PathsConfig:
    state_dir: Path
    socket_path: Path
    pid_file: Path
    log_file: Path
    preview_file: Path
    status_file: Path
    last_commit_file: Path


@dataclass
class AudioConfig:
    sample_rate: int
    channels: int
    sample_format: str
    read_chunk_ms: int
    capture_command: list[str]
    target: str | None


@dataclass
class ModelConfig:
    name: str
    device: str
    compute_type: str
    language: str | None
    beam_size: int
    vad_filter: bool
    condition_on_previous_text: bool
    cuda_lib_hints: bool


@dataclass
class StreamingConfig:
    default_mode: str
    chunk_ms: int
    window_seconds: float
    min_window_seconds: float
    stable_rounds: int
    live_commit: bool


@dataclass
class InjectionConfig:
    fallback_backends: list[str]
    append_trailing_space: bool


@dataclass
class SoundConfig:
    enabled: bool
    player: str
    events: dict[str, str]


@dataclass
class ProfileConfig:
    name: str
    default_mode: str | None
    chunk_ms: int | None
    window_seconds: float | None
    min_window_seconds: float | None
    stable_rounds: int | None
    live_commit: bool | None
    live_strategy: str | None
    inject_backends: list[str] | None
    append_trailing_space: bool | None


@dataclass
class AppConfig:
    root_dir: Path
    config_path: Path
    paths: PathsConfig
    audio: AudioConfig
    model: ModelConfig
    streaming: StreamingConfig
    injection: InjectionConfig
    sound: SoundConfig
    profiles_dir: Path
    default_profile: str


def normalize_text(text: str) -> str:
    return " ".join(text.strip().split())


def pcm_rms_percent(pcm_bytes: bytes) -> float:
    if len(pcm_bytes) < 2:
        return 0.0
    usable_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
    samples = np.frombuffer(pcm_bytes[:usable_len], dtype=np.int16)
    if samples.size == 0:
        return 0.0
    values = samples.astype(np.float32)
    rms = float(np.sqrt(np.mean(values * values)))
    return min(100.0, rms * 100.0 / 32768.0)


def tokenize(text: str) -> list[str]:
    norm = normalize_text(text)
    return norm.split(" ") if norm else []


def detokenize(tokens: list[str]) -> str:
    return " ".join(tokens)


def common_prefix_len(a: list[str], b: list[str]) -> int:
    idx = 0
    for left, right in zip(a, b):
        if left != right:
            break
        idx += 1
    return idx


def compare_token(token: str) -> str:
    return token.strip(".,!?;:\"'()[]{}<>`").casefold()


def common_prefix_len_fuzzy(a: list[str], b: list[str]) -> int:
    idx = 0
    for left, right in zip(a, b):
        if compare_token(left) != compare_token(right):
            break
        idx += 1
    return idx


def suffix_prefix_overlap_len_fuzzy(left: list[str], right: list[str]) -> int:
    max_len = min(len(left), len(right))
    for size in range(max_len, 0, -1):
        if common_prefix_len_fuzzy(left[-size:], right[:size]) == size:
            return size
    return 0


def trim_prefix_already_in_suffix(existing: list[str], candidate: list[str]) -> list[str]:
    overlap_len = suffix_prefix_overlap_len_fuzzy(existing, candidate)
    if overlap_len <= 0:
        return candidate
    return candidate[overlap_len:]


def rolling_append_final_remainder(
    committed_tokens: list[str],
    final_tokens: list[str],
    *,
    live_text_removal_ok: bool,
) -> tuple[list[str], str, int]:
    prefix_len = common_prefix_len_fuzzy(committed_tokens, final_tokens) if committed_tokens else 0
    if not committed_tokens:
        return list(final_tokens), "full_final", prefix_len
    if prefix_len == len(committed_tokens):
        return list(final_tokens[prefix_len:]), "append_remainder", prefix_len
    if live_text_removal_ok:
        return list(final_tokens), "replace_live_with_final", prefix_len
    return [], "skip_removal_failed", prefix_len


def common_prefix_len_many(items: list[list[str]]) -> int:
    if not items:
        return 0
    prefix_len = len(items[0])
    for tokens in items[1:]:
        prefix_len = min(prefix_len, common_prefix_len(items[0][:prefix_len], tokens))
    return prefix_len


def common_prefix_char_len(a: str, b: str) -> int:
    idx = 0
    for left, right in zip(a, b):
        if left != right:
            break
        idx += 1
    return idx


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def parse_mode(value: str) -> str:
    mode = value.strip().lower()
    if mode not in {"ptt", "live"}:
        raise ValueError(f"Invalid mode '{value}'. Expected ptt or live.")
    return mode


def normalize_input_target(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def normalize_backend_list(value: Any) -> list[str]:
    if value is None:
        return []

    raw_items: list[Any]
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = [value]

    deduped: list[str] = []
    for raw in raw_items:
        item = str(raw).strip().lower()
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_marker_target(value: Any) -> str:
    target = str(value or "auto").strip().lower()
    if target not in {"auto", "kitty", "system"}:
        raise ValueError(f"Invalid marker target '{value}'. Expected auto, kitty, or system.")
    return target


KITTY_CONTEXT_KEYS = ("KITTY_LISTEN_ON", "KITTY_WINDOW_ID", "KITTY_PUBLIC_KEY")
MARKER_CAPABLE_BACKENDS = {"kitty", "paste", "ydotool", "wtype", "xdotool"}
DEFAULT_SOUND_EVENTS = {
    "recording": "/usr/share/sounds/ocean/stereo/audio-volume-change.oga",
    "voice_ready": "/usr/share/sounds/ocean/stereo/button-pressed.oga",
    "finalizing": "/usr/share/sounds/ocean/stereo/completion-partial.oga",
    "committed": "/usr/share/sounds/ocean/stereo/completion-success.oga",
    "no_voice": "/usr/share/sounds/ocean/stereo/dialog-warning.oga",
    "no_audio": "/usr/share/sounds/ocean/stereo/dialog-warning.oga",
    "cancelled": "/usr/share/sounds/ocean/stereo/button-pressed-modifier.oga",
    "error": "/usr/share/sounds/ocean/stereo/dialog-error.oga",
}


def normalize_kitty_context(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}

    context: dict[str, str] = {}
    for key in KITTY_CONTEXT_KEYS:
        raw = value.get(key)
        if raw is None:
            raw = value.get(key.lower())
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            context[key] = text
    return context


def with_capture_target(base_cmd: list[str], target: str | None) -> list[str]:
    # Apply target by replacing any pre-existing --target in the configured command.
    cmd: list[str] = []
    idx = 0
    while idx < len(base_cmd):
        token = base_cmd[idx]
        if token == "--target":
            idx += 2
            continue
        cmd.append(token)
        idx += 1

    if not target:
        return cmd

    if "-" in cmd:
        dash_idx = cmd.index("-")
        return cmd[:dash_idx] + ["--target", target] + cmd[dash_idx:]

    return cmd + ["--target", target]


def load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def resolve_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def load_config(config_path: Path) -> AppConfig:
    config_path = config_path.resolve()
    cfg_dir = config_path.parent
    cfg = load_toml(config_path)

    paths_cfg = cfg.get("paths", {})
    state_dir = resolve_path(cfg_dir, paths_cfg.get("state_dir", "state"))
    paths = PathsConfig(
        state_dir=state_dir,
        socket_path=resolve_path(cfg_dir, paths_cfg.get("socket_path", "state/dictation.sock")),
        pid_file=resolve_path(cfg_dir, paths_cfg.get("pid_file", "state/dictation.pid")),
        log_file=resolve_path(cfg_dir, paths_cfg.get("log_file", "logs/dictation.log")),
        preview_file=resolve_path(cfg_dir, paths_cfg.get("preview_file", "state/preview.txt")),
        status_file=resolve_path(cfg_dir, paths_cfg.get("status_file", "state/status.json")),
        last_commit_file=resolve_path(
            cfg_dir, paths_cfg.get("last_commit_file", "state/last_commit.txt")
        ),
    )

    audio_cfg = cfg.get("audio", {})
    capture_command = audio_cfg.get("capture_command")
    if not capture_command:
        capture_command = [
            "pw-record",
            "--rate",
            str(audio_cfg.get("sample_rate", 16000)),
            "--channels",
            str(audio_cfg.get("channels", 1)),
            "--format",
            audio_cfg.get("sample_format", "s16"),
            "--raw",
            "-",
        ]

    audio = AudioConfig(
        sample_rate=int(audio_cfg.get("sample_rate", 16000)),
        channels=int(audio_cfg.get("channels", 1)),
        sample_format=str(audio_cfg.get("sample_format", "s16")),
        read_chunk_ms=int(audio_cfg.get("read_chunk_ms", 40)),
        capture_command=[str(x) for x in capture_command],
        target=normalize_input_target(audio_cfg.get("target")),
    )

    model_cfg = cfg.get("model", {})
    language = model_cfg.get("language")
    model = ModelConfig(
        name=str(model_cfg.get("name", "tiny.en")),
        device=str(model_cfg.get("device", "cuda")),
        compute_type=str(model_cfg.get("compute_type", "float16")),
        language=str(language) if language else None,
        beam_size=int(model_cfg.get("beam_size", 1)),
        vad_filter=bool(model_cfg.get("vad_filter", False)),
        condition_on_previous_text=bool(
            model_cfg.get("condition_on_previous_text", False)
        ),
        cuda_lib_hints=bool(model_cfg.get("cuda_lib_hints", True)),
    )

    streaming_cfg = cfg.get("streaming", {})
    streaming = StreamingConfig(
        default_mode=parse_mode(str(streaming_cfg.get("default_mode", "ptt"))),
        chunk_ms=int(streaming_cfg.get("chunk_ms", 400)),
        window_seconds=float(streaming_cfg.get("window_seconds", 8.0)),
        min_window_seconds=float(streaming_cfg.get("min_window_seconds", 1.2)),
        stable_rounds=max(1, int(streaming_cfg.get("stable_rounds", 2))),
        live_commit=bool(streaming_cfg.get("live_commit", True)),
    )

    injection_cfg = cfg.get("injection", {})
    injection = InjectionConfig(
        fallback_backends=[
            str(x).lower() for x in injection_cfg.get("fallback_backends", ["ydotool", "wtype", "stdout"])
        ],
        append_trailing_space=bool(injection_cfg.get("append_trailing_space", True)),
    )

    sound_cfg = cfg.get("sound", {})
    raw_sound_events = sound_cfg.get("events", {})
    sound_events = dict(DEFAULT_SOUND_EVENTS)
    if isinstance(raw_sound_events, dict):
        for event, path in raw_sound_events.items():
            text = str(path).strip()
            if text:
                sound_events[str(event).strip().lower()] = text
    sound = SoundConfig(
        enabled=bool(sound_cfg.get("enabled", True)),
        player=str(sound_cfg.get("player", "paplay")),
        events=sound_events,
    )

    profiles_cfg = cfg.get("profiles", {})
    profiles_dir = resolve_path(cfg_dir, profiles_cfg.get("dir", "profiles"))
    default_profile = str(profiles_cfg.get("default", "codex"))

    return AppConfig(
        root_dir=cfg_dir,
        config_path=config_path,
        paths=paths,
        audio=audio,
        model=model,
        streaming=streaming,
        injection=injection,
        sound=sound,
        profiles_dir=profiles_dir,
        default_profile=default_profile,
    )


def load_profile(path: Path, name: str) -> ProfileConfig:
    profile_path = path / f"{name}.toml"
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile '{name}' not found: {profile_path}")

    data = load_toml(profile_path)
    return ProfileConfig(
        name=str(data.get("name", name)),
        default_mode=parse_mode(data["default_mode"]) if "default_mode" in data else None,
        chunk_ms=int(data["chunk_ms"]) if "chunk_ms" in data else None,
        window_seconds=float(data["window_seconds"]) if "window_seconds" in data else None,
        min_window_seconds=float(data["min_window_seconds"])
        if "min_window_seconds" in data
        else None,
        stable_rounds=max(1, int(data["stable_rounds"])) if "stable_rounds" in data else None,
        live_commit=bool(data["live_commit"]) if "live_commit" in data else None,
        live_strategy=str(data["live_strategy"]).strip().lower()
        if "live_strategy" in data
        else None,
        inject_backends=[str(x).lower() for x in data.get("inject_backends", [])]
        if "inject_backends" in data
        else None,
        append_trailing_space=bool(data["append_trailing_space"])
        if "append_trailing_space" in data
        else None,
    )


class TextInjector:
    FALLBACK_BIN_DIRS = (Path("/usr/bin"), Path("/bin"), Path("/usr/local/bin"))

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.kitty_context: dict[str, str] = {}

    def set_kitty_context(self, context: dict[str, str]) -> None:
        self.kitty_context = {
            key: value
            for key, value in context.items()
            if key in KITTY_CONTEXT_KEYS and value
        }

    def clear_kitty_context(self) -> None:
        self.kitty_context = {}

    def _resolve_binary(self, name: str) -> str | None:
        found = shutil.which(name)
        if found:
            return found

        for root in self.FALLBACK_BIN_DIRS:
            candidate = root / name
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    def _kitty_listen_on(self) -> str:
        return self.kitty_context.get("KITTY_LISTEN_ON") or os.environ.get("KITTY_LISTEN_ON", "")

    def _kitty_env(self) -> dict[str, str] | None:
        if not self.kitty_context:
            return None
        env = os.environ.copy()
        env.update(self.kitty_context)
        return env

    def available_backends(self) -> dict[str, bool]:
        wl_copy = self._resolve_binary("wl-copy")
        xclip = self._resolve_binary("xclip")
        return {
            "kitty": self._resolve_binary("kitten") is not None
            and bool(self._kitty_listen_on()),
            "wtype": self._resolve_binary("wtype") is not None,
            "ydotool": self._resolve_binary("ydotool") is not None,
            "xdotool": self._resolve_binary("xdotool") is not None,
            "wl-copy": wl_copy is not None,
            "xclip": xclip is not None,
            "clipboard": wl_copy is not None or xclip is not None,
            "stdout": True,
        }

    def inject(self, text: str, backends: list[str]) -> tuple[bool, str, str]:
        if not backends:
            return False, "none", "No injector backends configured."

        errors: list[str] = []
        for backend in backends:
            method = getattr(self, f"_inject_{backend}", None)
            if method is None:
                errors.append(f"{backend}: unsupported backend")
                continue
            ok, message = method(text)
            if ok:
                return True, backend, message
            errors.append(f"{backend}: {message}")
            self.logger.debug("Injector backend failed: %s (%s)", backend, message)

        if errors:
            return False, "none", "No configured injector backend succeeded. " + " | ".join(errors)
        return False, "none", "No configured injector backend succeeded."

    def backspace(self, backends: list[str], count: int = 1) -> tuple[bool, str, str]:
        if not backends:
            return False, "none", "No injector backends configured."

        count = max(1, count)
        errors: list[str] = []
        for backend in backends:
            method = getattr(self, f"_backspace_{backend}", None)
            if method is None:
                errors.append(f"{backend}: unsupported backspace backend")
                continue
            ok, message = method(count)
            if ok:
                return True, backend, message
            errors.append(f"{backend}: {message}")
            self.logger.debug("Backspace backend failed: %s (%s)", backend, message)

        if errors:
            return False, "none", "No configured backspace backend succeeded. " + " | ".join(errors)
        return False, "none", "No configured backspace backend succeeded."

    def _inject_wtype(self, text: str) -> tuple[bool, str]:
        binary = self._resolve_binary("wtype")
        if binary is None:
            return False, "wtype binary not found"
        return self._run([binary, text], "wtype")

    def _inject_ydotool(self, text: str) -> tuple[bool, str]:
        binary = self._resolve_binary("ydotool")
        if binary is None:
            return False, "ydotool binary not found"
        return self._run(
            [binary, "type", "--key-delay", "4", "--key-hold", "4", text],
            "ydotool",
        )

    def _inject_xdotool(self, text: str) -> tuple[bool, str]:
        binary = self._resolve_binary("xdotool")
        if binary is None:
            return False, "xdotool binary not found"
        return self._run(
            [binary, "type", "--clearmodifiers", "--delay", "1", text], "xdotool"
        )

    def _inject_kitty(self, text: str) -> tuple[bool, str]:
        binary = self._resolve_binary("kitten")
        listen_on = self._kitty_listen_on()
        if binary is None:
            return False, "kitten binary not found"
        if not listen_on:
            return False, "KITTY_LISTEN_ON is not set"
        return self._run(
            [
                binary,
                "@",
                "--to",
                listen_on,
                "send-text",
                "--match",
                "state:focused",
                "--stdin",
            ],
            "kitty-send-text",
            input_text=text,
            env=self._kitty_env(),
        )

    def _backspace_ydotool(self, count: int = 1) -> tuple[bool, str]:
        binary = self._resolve_binary("ydotool")
        if binary is None:
            return False, "ydotool binary not found"
        # KEY_BACKSPACE=14 (linux/input-event-codes.h)
        keys: list[str] = []
        for _ in range(max(1, count)):
            keys.extend(["14:1", "14:0"])
        return self._run(
            [binary, "key", "--key-delay", str(YDOTOOL_BACKSPACE_KEY_DELAY_MS), *keys],
            "ydotool-backspace",
        )

    def _backspace_xdotool(self, count: int = 1) -> tuple[bool, str]:
        binary = self._resolve_binary("xdotool")
        if binary is None:
            return False, "xdotool binary not found"
        keys = ["BackSpace"] * max(1, count)
        return self._run(
            [
                binary,
                "key",
                "--clearmodifiers",
                "--delay",
                str(XDOTOOL_BACKSPACE_DELAY_MS),
                *keys,
            ],
            "xdotool-backspace",
        )

    def _backspace_kitty(self, count: int = 1) -> tuple[bool, str]:
        binary = self._resolve_binary("kitten")
        listen_on = self._kitty_listen_on()
        if binary is None:
            return False, "kitten binary not found"
        if not listen_on:
            return False, "KITTY_LISTEN_ON is not set"
        keys = ["backspace"] * max(1, count)
        return self._run(
            [
                binary,
                "@",
                "--to",
                listen_on,
                "send-key",
                "--match",
                "state:focused",
                *keys,
            ],
            "kitty-send-key",
            env=self._kitty_env(),
        )

    def _inject_paste(self, text: str) -> tuple[bool, str]:
        xdotool = self._resolve_binary("xdotool")
        ydotool = self._resolve_binary("ydotool")
        if xdotool is None and ydotool is None:
            return False, "paste backend requires xdotool or ydotool for paste keypress"

        copied, copy_message = self._copy_to_clipboard(text)
        if not copied:
            return False, copy_message

        if xdotool is not None:
            ok, key_message = self._run([xdotool, "key", "--clearmodifiers", "ctrl+v"], "paste")
        else:
            # KEY_LEFTCTRL=29, KEY_V=47 (linux/input-event-codes.h)
            ok, key_message = self._run([ydotool, "key", "29:1", "47:1", "47:0", "29:0"], "paste")

        if not ok:
            return False, key_message
        return True, f"{copy_message}; {key_message}"

    def _inject_clipboard(self, text: str) -> tuple[bool, str]:
        return self._copy_to_clipboard(text)

    def _copy_to_clipboard(self, text: str) -> tuple[bool, str]:
        wl_copy = self._resolve_binary("wl-copy")
        xclip = self._resolve_binary("xclip")
        candidates: list[tuple[str, list[str]]] = []
        if wl_copy is not None:
            candidates.append(("wl-copy", [wl_copy]))
        if xclip is not None:
            candidates.append(("xclip", [xclip, "-selection", "clipboard"]))

        if not candidates:
            return False, "clipboard backend requires wl-copy or xclip"

        errors: list[str] = []
        for label, copy_cmd in candidates:
            try:
                # Use DEVNULL for stdio to avoid deadlock when clipboard tools daemonize.
                copy_proc = subprocess.run(
                    copy_cmd,
                    input=text,
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"{label}: timed out")
                continue
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(f"{label}: exec error: {exc}")
                continue

            if copy_proc.returncode == 0:
                return True, f"clipboard ok ({label})"
            errors.append(f"{label}: exited with {copy_proc.returncode}")

        return False, "clipboard command failed: " + " | ".join(errors)

    def _inject_stdout(self, text: str) -> tuple[bool, str]:
        print(text, flush=True)
        return True, "printed to stdout"

    @staticmethod
    def _run(
        cmd: list[str],
        label: str,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                env=env,
            )
        except Exception as exc:  # pragma: no cover - defensive
            return False, f"{label} exec error: {exc}"

        if proc.returncode != 0:
            stderr = proc.stderr.strip() or proc.stdout.strip()
            return False, f"{label} exited with {proc.returncode}: {stderr}"
        return True, f"{label} ok"


class SoundPlayer:
    def __init__(self, config: SoundConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.player_binary = shutil.which(config.player) if config.player else None
        if self.player_binary is None:
            for fallback in ("paplay", "pw-play", "canberra-gtk-play"):
                self.player_binary = shutil.which(fallback)
                if self.player_binary is not None:
                    break

    def play(self, event: str) -> None:
        if not self.config.enabled or self.player_binary is None:
            return

        sound_path = self.config.events.get(event)
        if not sound_path:
            return
        path = Path(sound_path)
        if not path.exists():
            self.logger.debug("Sound cue missing for event=%s path=%s", event, path)
            return

        try:
            subprocess.Popen(
                [self.player_binary, str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:  # pragma: no cover - best-effort cue
            self.logger.debug("Sound cue failed for event=%s: %s", event, exc)


class AudioRecorder:
    def __init__(
        self,
        audio_cfg: AudioConfig,
        logger: logging.Logger,
        on_chunk: callable,
        capture_command: list[str] | None = None,
    ):
        self.audio_cfg = audio_cfg
        self.capture_command = list(capture_command or audio_cfg.capture_command)
        self.logger = logger
        self.on_chunk = on_chunk
        self.proc: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop = threading.Event()

        bytes_per_sample = 2 if audio_cfg.sample_format == "s16" else 2
        self.chunk_bytes = max(
            320,
            int(
                audio_cfg.sample_rate
                * audio_cfg.channels
                * bytes_per_sample
                * audio_cfg.read_chunk_ms
                / 1000
            ),
        )

    def start(self) -> None:
        if not self.capture_command:
            raise RuntimeError("Capture command is empty")
        cmd = self.capture_command
        binary = cmd[0]
        if shutil.which(binary) is None:
            raise RuntimeError(f"Capture binary not found: {binary}")

        self.logger.info("Starting recorder: %s", " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            bufsize=0,
        )
        self._stop.clear()

        self._reader_thread = threading.Thread(target=self._reader_loop, name="audio-reader", daemon=True)
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(target=self._stderr_loop, name="audio-stderr", daemon=True)
        self._stderr_thread.start()

    def stop(self) -> float:
        started = time.perf_counter()
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                self.logger.warning("Recorder did not exit on SIGTERM quickly; sending SIGKILL")
                self.proc.kill()
                try:
                    self.proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    self.logger.warning("Recorder still not exited after SIGKILL")

        if self._reader_thread:
            self._reader_thread.join(timeout=0.25)
        if self._stderr_thread:
            self._stderr_thread.join(timeout=0.25)

        return time.perf_counter() - started

    def _reader_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while not self._stop.is_set():
            data = self.proc.stdout.read(self.chunk_bytes)
            if not data:
                break
            self.on_chunk(data)

    def _stderr_loop(self) -> None:
        assert self.proc and self.proc.stderr
        while not self._stop.is_set():
            line = self.proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self.logger.debug("pw-record: %s", text)


class DictationDaemon:
    def __init__(self, config: AppConfig):
        self.config = config

        self.config.paths.state_dir.mkdir(parents=True, exist_ok=True)
        ensure_parent(self.config.paths.log_file)
        ensure_parent(self.config.paths.preview_file)
        ensure_parent(self.config.paths.status_file)
        ensure_parent(self.config.paths.last_commit_file)
        ensure_parent(self.config.paths.pid_file)

        self.logger = self._build_logger()
        self.injector = TextInjector(self.logger)
        self.sound_player = SoundPlayer(self.config.sound, self.logger)

        self.state_lock = threading.Lock()
        self.model_lock = threading.Lock()
        self.marker_lock = threading.RLock()
        self.shutdown_event = threading.Event()

        self.session_active = False
        self.session_started_at = 0.0
        self.session_audio = bytearray()
        self.ring_chunks: deque[bytes] = deque()
        self.ring_total_bytes = 0

        self.capture: AudioRecorder | None = None
        self.session_stop_event: threading.Event | None = None
        self.live_thread: threading.Thread | None = None
        self.marker_thread: threading.Thread | None = None

        self.last_error = ""
        self.last_commit_text = ""
        self.last_injected_backend = ""
        self.last_injection_message = ""
        self.last_result: dict[str, Any] = {
            "kind": "none",
            "source": "",
            "reason": "",
            "text": "",
            "injected_text": "",
            "backend": "",
            "message": "",
            "injection_ok": None,
            "elapsed_s": None,
            "details": {},
            "updated_at": time.time(),
        }
        self.activity_state = "idle"
        self.activity_updated_at = time.time()

        self.profile_name = self.config.default_profile
        self.profile = load_profile(self.config.profiles_dir, self.profile_name)

        self.mode = self.config.streaming.default_mode
        self.chunk_ms = self.config.streaming.chunk_ms
        self.window_seconds = self.config.streaming.window_seconds
        self.min_window_seconds = self.config.streaming.min_window_seconds
        self.stable_rounds = self.config.streaming.stable_rounds
        self.live_commit = self.config.streaming.live_commit
        self.live_strategy = "prefix"
        self.inject_backends = list(self.config.injection.fallback_backends)
        self.append_trailing_space = self.config.injection.append_trailing_space
        self.input_target = self.config.audio.target
        self.input_target_file = self.config.paths.state_dir / "input_target.txt"
        if self.input_target_file.exists():
            persisted = normalize_input_target(
                self.input_target_file.read_text(encoding="utf-8")
            )
            if persisted is not None:
                self.input_target = persisted

        self.session_input_target: str | None = None
        self.session_marker_target = "auto"
        self.session_kitty_context: dict[str, str] = {}
        self.session_inject_backends: list[str] | None = None
        self.session_inline_markers = False
        self.session_debug_streams = False
        self.last_debug_streams = False
        self.debug_stream_file = self.config.paths.state_dir / "debug_streams.txt"
        self.external_session_active = False
        self.external_session_id = ""
        self.external_session_source = ""
        self.external_event_seq = -1
        self.display_session_counter = 0
        self.display_session_id = ""
        self.display_source = "local_whisper"
        self.display_seq = 0
        self.display_updated_at = time.time()
        self.display_state = "idle"
        self.display_note = "idle"
        self.display_committed_text = ""
        self.display_partial_text = ""
        self.display_final_text = ""
        self.display_partial_is_mutable = True

        self._live_prev_tokens: list[str] = []
        self._live_candidate_tokens: list[str] = []
        self._live_candidate_count = 0
        self._live_committed_tokens: list[str] = []
        self._live_injected_text = ""
        self._live_append_window_tokens: list[str] = []
        self._live_pending_append_tokens: list[str] = []
        self._live_alignment_lost = False
        self._live_input_marker_visible = False
        self._live_input_marker_text = ""
        self._live_input_marker_state = "idle"
        self._live_last_injected_at = 0.0
        self._live_level_marker_updated_at = 0.0
        self.audio_level_percent = 0.0
        self.session_peak_level_percent = 0.0
        self.session_voice_seen = False
        self.session_last_voice_seen_at = 0.0
        self.session_voice_chunk_count = 0
        self.session_strong_voice_chunk_count = 0
        self._debug_stream_events: deque[str] = deque(maxlen=12)
        self._debug_stream_snapshots: deque[dict[str, str]] = deque(maxlen=8)
        self._debug_policy_states: dict[str, dict[str, Any]] = {}
        self._debug_branch_expression = ""
        self._debug_final_expression = ""
        self._debug_branch_rows: dict[str, str] = {}
        self._debug_final_rows: dict[str, str] = {}

        self.bytes_per_second = self.config.audio.sample_rate * self.config.audio.channels * 2
        self.ring_max_bytes = max(1, int(self.window_seconds * self.bytes_per_second))
        self.min_window_bytes = max(1, int(self.min_window_seconds * self.bytes_per_second))
        self._apply_profile(self.profile)

        self.model: WhisperModel | None = None
        self.active_model_device = self.config.model.device
        self.active_model_compute_type = self.config.model.compute_type

        self._write_preview("", "", note="idle")
        self._persist_status()

    def _build_logger(self) -> logging.Logger:
        logger = logging.getLogger("dictationd")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()

        file_handler = logging.FileHandler(self.config.paths.log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
        )
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(stream_handler)

        return logger

    def run(self) -> int:
        self.logger.info("Starting dictation daemon")
        self._write_pid()

        try:
            self._load_model()
        except Exception as exc:
            self.last_error = f"Model load failed: {exc}"
            self._persist_status()
            self.logger.exception("Model load failed")
            self._cleanup_pid()
            return 1

        if self.config.paths.socket_path.exists():
            self.config.paths.socket_path.unlink()

        server = ControlServer(str(self.config.paths.socket_path), ControlHandler)
        server.daemon_ref = self

        server_thread = threading.Thread(target=server.serve_forever, name="control-server", daemon=True)
        server_thread.start()

        self.logger.info("Control socket ready: %s", self.config.paths.socket_path)
        self._persist_status()

        try:
            while not self.shutdown_event.is_set():
                time.sleep(0.2)
        finally:
            self.logger.info("Shutting down dictation daemon")
            if self.session_active:
                try:
                    self._stop_session(commit=False)
                except Exception:
                    self.logger.exception("Failed to stop active session during shutdown")

            server.shutdown()
            server.server_close()
            if self.config.paths.socket_path.exists():
                self.config.paths.socket_path.unlink()
            self._cleanup_pid()
            self._write_preview("", "", note="stopped")
            self._persist_status()

        return 0

    def handle_command(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = str(request.get("cmd", "")).strip().lower()

        try:
            if cmd == "status":
                return {"ok": True, "status": self._status_payload()}
            if cmd == "start":
                target: Any = UNSET
                if "input_target" in request:
                    target = normalize_input_target(request.get("input_target"))
                marker_target = parse_marker_target(request.get("marker_target", "auto"))
                kitty_context = normalize_kitty_context(request.get("kitty_context"))
                debug_streams = parse_bool(request.get("debug_streams"), default=False)
                return self._start_session(target, marker_target, kitty_context, debug_streams)
            if cmd == "stop_commit":
                return self._stop_session(commit=True)
            if cmd == "stop_cancel":
                return self._stop_session(commit=False)
            if cmd == "toggle_live":
                return self._toggle_live()
            if cmd == "set_mode":
                mode = parse_mode(str(request.get("mode", "")))
                return self._set_mode(mode)
            if cmd == "set_input_target":
                target = normalize_input_target(request.get("input_target"))
                return self._set_input_target(target)
            if cmd == "switch_profile":
                profile_name = str(request.get("profile", "")).strip()
                return self._switch_profile(profile_name)
            if cmd == "inject_text":
                text = str(request.get("text", ""))
                backends = normalize_backend_list(request.get("backends"))
                append_override = request.get("append_trailing_space")
                if append_override is None:
                    append_trailing_space = None
                elif isinstance(append_override, bool):
                    append_trailing_space = append_override
                else:
                    append_trailing_space = str(append_override).strip().lower() in {
                        "1",
                        "true",
                        "yes",
                        "on",
                    }
                return self._inject_text_command(
                    text,
                    backends if backends else None,
                    append_trailing_space=append_trailing_space,
                )
            if cmd == "external_event":
                return self._handle_external_event(request)
            if cmd == "tail":
                text = self.config.paths.preview_file.read_text(encoding="utf-8")
                return {"ok": True, "preview": text}
            if cmd == "simulate_divergence":
                backends = (
                    normalize_backend_list(request.get("backends"))
                    if "backends" in request
                    else None
                )
                append_trailing_space: Any = UNSET
                if "append_trailing_space" in request:
                    append_trailing_space = parse_bool(
                        request.get("append_trailing_space"),
                        self.append_trailing_space,
                    )
                return self._simulate_divergence(
                    live_text=str(request.get("live") or ""),
                    final_text=str(request.get("final") or ""),
                    confirm_delete=parse_bool(request.get("confirm_delete"), False),
                    backends=backends,
                    append_trailing_space=append_trailing_space,
                )

            if cmd == "shutdown":
                self.shutdown_event.set()
                return {"ok": True, "message": "shutdown requested"}

            return {"ok": False, "error": f"Unknown command: {cmd}"}
        except Exception as exc:
            self.last_error = str(exc)
            self.logger.exception("Command failed: %s", cmd)
            self._persist_status()
            return {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}

    def _status_payload(self) -> dict[str, Any]:
        return {
            "running": not self.shutdown_event.is_set(),
            "recording": self.session_active,
            "external_session_active": self.external_session_active,
            "external_session_id": self.external_session_id,
            "external_session_source": self.external_session_source,
            "mode": self.mode,
            "profile": self.profile_name,
            "chunk_ms": self.chunk_ms,
            "window_seconds": self.window_seconds,
            "stable_rounds": self.stable_rounds,
            "live_commit": self.live_commit,
            "live_strategy": self.live_strategy,
            "input_target": self.input_target,
            "session_input_target": self.session_input_target,
            "model": {
                "name": self.config.model.name,
                "requested_device": self.config.model.device,
                "requested_compute_type": self.config.model.compute_type,
                "active_device": self.active_model_device,
                "active_compute_type": self.active_model_compute_type,
            },
            "inject_backends": self.inject_backends,
            "available_backends": self.injector.available_backends(),
            "last_error": self.last_error,
            "last_injected_backend": self.last_injected_backend,
            "last_injection_message": self.last_injection_message,
            "activity_state": self.activity_state,
            "activity_updated_at": self.activity_updated_at,
            "live_input_marker": self._live_input_marker_text or LIVE_INPUT_MARKER,
            "session_marker_target": self.session_marker_target,
            "session_kitty_context": bool(self.session_kitty_context),
            "session_inject_backends": self.session_inject_backends,
            "session_inline_markers": self.session_inline_markers,
            "session_debug_streams": self.session_debug_streams,
            "last_debug_streams": self.last_debug_streams,
            "debug_branch_preview": self._debug_branch_expression[:240],
            "live_alignment_lost": self._live_alignment_lost,
            "live_input_marker_state": self._live_input_marker_state,
            "live_input_marker_visible": self._live_input_marker_visible,
            "live_input_markers": LIVE_MARKERS,
            "audio_level_percent": round(self.audio_level_percent, 2),
            "session_peak_level_percent": round(self.session_peak_level_percent, 2),
            "audio_level_threshold_percent": AUDIO_LEVEL_THRESHOLD_PERCENT,
            "audio_level_strong_threshold_percent": AUDIO_LEVEL_STRONG_THRESHOLD_PERCENT,
            "audio_level_ready_min_chunks": AUDIO_LEVEL_READY_MIN_CHUNKS,
            "audio_level_ready_min_strong_chunks": AUDIO_LEVEL_READY_MIN_STRONG_CHUNKS,
            "audio_level_display_max_percent": AUDIO_LEVEL_DISPLAY_MAX_PERCENT,
            "audio_level_display_block": LIVE_VOLUME_BLOCKS[self._audio_level_bucket()],
            "audio_level_grace_seconds": AUDIO_LEVEL_GRACE_SECONDS,
            "session_voice_seen": self.session_voice_seen,
            "session_voice_chunk_count": self.session_voice_chunk_count,
            "session_strong_voice_chunk_count": self.session_strong_voice_chunk_count,
            "audio_gate_accepting": self.session_active and self._session_voice_ready(),
            "audio_gate_label": self._audio_gate_label(),
            "last_commit_preview": self.last_commit_text[:160],
            "last_result": dict(self.last_result),
            "dictation_display": self._dictation_display_payload(),
            "paths": {
                "preview_file": str(self.config.paths.preview_file),
                "status_file": str(self.config.paths.status_file),
                "log_file": str(self.config.paths.log_file),
                "socket_path": str(self.config.paths.socket_path),
            },
        }

    def _set_last_result(
        self,
        kind: str,
        *,
        source: str = "local_whisper",
        reason: str = "",
        text: str = "",
        injected_text: str = "",
        backend: str = "",
        message: str = "",
        injection_ok: bool | None = None,
        elapsed_s: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.last_result = {
            "kind": kind,
            "source": source,
            "reason": reason,
            "text": normalize_text(text),
            "injected_text": str(injected_text or ""),
            "backend": str(backend or ""),
            "message": str(message or ""),
            "injection_ok": injection_ok,
            "elapsed_s": round(elapsed_s, 3) if elapsed_s is not None else None,
            "activity": self.activity_state,
            "mode": self.mode,
            "profile": self.profile_name,
            "details": details or {},
            "updated_at": time.time(),
        }

    def _dictation_display_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.display_session_id,
            "source": self.display_source,
            "state": self.display_state,
            "note": self.display_note,
            "seq": self.display_seq,
            "updated_at": self.display_updated_at,
            "committed_text": self.display_committed_text,
            "partial_text": self.display_partial_text,
            "final_text": self.display_final_text,
            "partial_is_mutable": self.display_partial_is_mutable,
            "alignment_lost": self._live_alignment_lost,
            "audio_gate_label": self._audio_gate_label(),
            "audio_level_percent": round(self.audio_level_percent, 2),
            "session_peak_level_percent": round(self.session_peak_level_percent, 2),
        }

    def _display_state_for_note(self, note: str) -> str:
        text = note.strip().lower()
        if self.session_active:
            return "listening"
        if text == "committed":
            return "committed"
        if text == "cancelled":
            return "cancelled"
        if text == "low volume / no voice":
            return "no_voice"
        if text == "no audio captured":
            return "no_audio"
        if text == "stopped":
            return "stopped"
        if self.activity_state == "finalizing":
            return "finalizing"
        if self.activity_state == "error":
            return "error"
        return "idle"

    def _update_dictation_display(self, committed: str, partial: str, note: str) -> None:
        state = self._display_state_for_note(note)
        final_text = committed if state == "committed" else ""
        self.display_seq += 1
        self.display_updated_at = time.time()
        self.display_source = "local_whisper"
        self.display_state = state
        self.display_note = note
        self.display_committed_text = normalize_text(committed)
        self.display_partial_text = normalize_text(partial) if self.session_active else ""
        self.display_final_text = normalize_text(final_text)
        self.display_partial_is_mutable = self.session_active

    def _parse_external_seq(self, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return self.external_event_seq + 1

    def _set_display_state(
        self,
        *,
        source: str,
        session_id: str,
        state: str,
        note: str,
        committed: str = "",
        partial: str = "",
        final: str = "",
        mutable: bool = False,
        event_seq: int | None = None,
    ) -> None:
        if event_seq is None:
            self.display_seq += 1
        else:
            self.display_seq = max(self.display_seq + 1, event_seq)
        self.display_updated_at = time.time()
        self.display_source = source
        self.display_session_id = session_id
        self.display_state = state
        self.display_note = note
        self.display_committed_text = normalize_text(committed)
        self.display_partial_text = normalize_text(partial)
        self.display_final_text = normalize_text(final)
        self.display_partial_is_mutable = mutable

    def _write_preview_file(self, committed: str, tail: str, note: str) -> None:
        ensure_parent(self.config.paths.preview_file)
        self.config.paths.preview_file.write_text(
            self._format_preview(committed, tail, note), encoding="utf-8"
        )

    def _write_external_preview(self, committed: str, partial: str, note: str) -> None:
        self._write_preview_file(committed, partial, note)

    def _handle_external_event(self, request: dict[str, Any]) -> dict[str, Any]:
        event_type = str(request.get("type", "")).strip().lower()
        session_id = str(request.get("session_id", "")).strip()
        source = str(request.get("source", "android")).strip() or "android"
        event_seq = self._parse_external_seq(request.get("seq"))
        text = normalize_text(str(request.get("text", "")))

        if event_type not in {"start", "partial", "final", "cancel", "error", "end"}:
            return {"ok": False, "error": f"unknown external event type: {event_type}"}
        if not session_id:
            return {"ok": False, "error": "external session_id is required"}

        if event_seq < self.external_event_seq and session_id == self.external_session_id:
            return {
                "ok": True,
                "message": "stale external event ignored",
                "session_id": session_id,
                "seq": event_seq,
            }

        if event_type == "start":
            if self.session_active:
                return {"ok": False, "error": "local recording is active"}
            if self.external_session_active and session_id != self.external_session_id:
                return {
                    "ok": False,
                    "error": f"external session already active: {self.external_session_id}",
                }
            self.external_session_active = True
            self.external_session_id = session_id
            self.external_session_source = source
            self.external_event_seq = event_seq
            self.last_error = ""
            self._set_activity("recording", persist=False)
            self._set_display_state(
                source=source,
                session_id=session_id,
                state="listening",
                note="external recording",
                mutable=True,
                event_seq=event_seq,
            )
            self._write_external_preview("", "", "external recording")
            self._persist_status()
            self.logger.info("External dictation started source=%s session=%s", source, session_id)
            return {"ok": True, "message": "external session started", "session_id": session_id}

        if not self.external_session_active or session_id != self.external_session_id:
            return {"ok": False, "error": "no matching external session active"}

        self.external_event_seq = event_seq

        if event_type == "partial":
            self.activity_state = "recording"
            self.activity_updated_at = time.time()
            self._set_display_state(
                source=source,
                session_id=session_id,
                state="listening",
                note="external partial",
                partial=text,
                mutable=True,
                event_seq=event_seq,
            )
            self._write_external_preview("", text, "external partial")
            self._persist_status()
            return {"ok": True, "message": "external partial accepted", "session_id": session_id}

        if event_type == "final":
            self._set_activity("finalizing", persist=False)
            append_override = request.get("append_trailing_space")
            if append_override is None:
                append_trailing_space = None
            elif isinstance(append_override, bool):
                append_trailing_space = append_override
            else:
                append_trailing_space = str(append_override).strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
            result = self._inject_text(text, append_trailing_space=append_trailing_space)
            if result["ok"]:
                self.last_commit_text = text
                self.last_error = ""
                self.config.paths.last_commit_file.write_text(text, encoding="utf-8")
                self._set_activity("committed", persist=False)
                state = "committed"
                note = "external committed"
            else:
                self._set_activity("error", persist=False)
                state = "error"
                note = "external injection failed"
            self._set_last_result(
                "external_committed" if result["ok"] else "external_injection_failed",
                source=source,
                reason=note,
                text=text,
                injected_text=str(result.get("payload") or ""),
                backend=str(result.get("backend") or ""),
                message=str(result.get("message") or ""),
                injection_ok=bool(result["ok"]),
                details={"session_id": session_id, "seq": event_seq},
            )
            self.external_session_active = False
            self.external_session_id = ""
            self.external_session_source = ""
            self._set_display_state(
                source=source,
                session_id=session_id,
                state=state,
                note=note,
                committed=text,
                final=text,
                mutable=False,
                event_seq=event_seq,
            )
            self._write_external_preview(text, "", note)
            self._persist_status()
            return {
                "ok": result["ok"],
                "message": note,
                "session_id": session_id,
                "backend": result.get("backend"),
                "text": text,
            }

        if event_type == "cancel":
            self.external_session_active = False
            self.external_session_id = ""
            self.external_session_source = ""
            self._set_activity("cancelled", persist=False)
            self._set_last_result(
                "external_cancelled",
                source=source,
                reason="external session cancelled",
                details={"session_id": session_id, "seq": event_seq},
            )
            self._set_display_state(
                source=source,
                session_id=session_id,
                state="cancelled",
                note="external cancelled",
                mutable=False,
                event_seq=event_seq,
            )
            self._write_external_preview("", "", "external cancelled")
            self._persist_status()
            return {"ok": True, "message": "external session cancelled", "session_id": session_id}

        if event_type == "error":
            error = str(request.get("error", "external dictation error")).strip()
            self.external_session_active = False
            self.external_session_id = ""
            self.external_session_source = ""
            self.last_error = error
            self._set_activity("error", persist=False)
            self._set_last_result(
                "external_error",
                source=source,
                reason=error,
                message=error,
                injection_ok=False,
                details={"session_id": session_id, "seq": event_seq},
            )
            self._set_display_state(
                source=source,
                session_id=session_id,
                state="error",
                note=error,
                mutable=False,
                event_seq=event_seq,
            )
            self._write_external_preview("", "", error)
            self._persist_status()
            return {"ok": True, "message": "external error recorded", "session_id": session_id}

        self.external_session_active = False
        self.external_session_id = ""
        self.external_session_source = ""
        self._set_activity("idle", persist=False)
        self._set_last_result(
            "external_ended",
            source=source,
            reason="external session ended without final text",
            details={"session_id": session_id, "seq": event_seq},
        )
        self._set_display_state(
            source=source,
            session_id=session_id,
            state="idle",
            note="external ended",
            mutable=False,
            event_seq=event_seq,
        )
        self._write_external_preview("", "", "external ended")
        self._persist_status()
        return {"ok": True, "message": "external session ended", "session_id": session_id}

    def _persist_status(self) -> None:
        payload = self._status_payload()
        payload["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
        ensure_parent(self.config.paths.status_file)
        self.config.paths.status_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8"
        )

    def _write_preview(self, committed: str, tail: str, note: str) -> None:
        self._update_dictation_display(committed, tail, note)
        self._write_preview_file(committed, tail, note)

    def _preview_row(self, label: str, value: str, width: int = PREVIEW_TEXT_WIDTH) -> str:
        label_text = f"{label:<22}"[:22]
        value_text = normalize_text(value)
        if len(value_text) > width:
            value_text = value_text[: max(0, width - 3)].rstrip() + "..."
        return f"{label_text} {value_text:<{width}}"

    def _preview_text_rows(
        self,
        label: str,
        value: str,
        rows: int,
        width: int = PREVIEW_TEXT_WIDTH,
    ) -> list[str]:
        text = normalize_text(value)
        wrapped = textwrap.wrap(text, width=width) if text else []
        lines = [f"{label}"]
        for idx in range(rows):
            content = wrapped[idx] if idx < len(wrapped) else ""
            lines.append(f"  {idx + 1:02d} {content:<{width}}")
        return lines

    def _format_preview(self, committed: str, tail: str, note: str) -> str:
        lines = [
            "personal-stt preview",
            self._preview_row("mode", self.mode),
            self._preview_row("profile", self.profile_name),
            self._preview_row("recording", str(self.session_active)),
            self._preview_row("activity", self.activity_state),
            self._preview_row("note", note),
            self._preview_row("audio_gate", self._audio_gate_label()),
            self._preview_row("level", f"{self.audio_level_percent:.2f}% peak={self.session_peak_level_percent:.2f}%"),
            self._preview_row(
                "params",
                (
                    f"window={self.window_seconds} min={self.min_window_seconds} "
                    f"chunk_ms={self.chunk_ms} stable_rounds={self.stable_rounds}"
                ),
            ),
        ]
        lines.extend(self._preview_text_rows("committed", committed, PREVIEW_COMMITTED_ROWS))
        lines.extend(self._preview_text_rows("tail", tail, PREVIEW_TAIL_ROWS))
        lines.extend(self._format_debug_streams())
        return "\n".join(lines) + "\n"

    def _session_live_commit_enabled(self) -> bool:
        return self.live_commit and not self.session_debug_streams

    def _reset_debug_streams(self) -> None:
        self._debug_stream_events = deque(maxlen=12)
        self._debug_stream_snapshots = deque(maxlen=8)
        self._debug_policy_states = {}
        self._debug_branch_expression = ""
        self._debug_final_expression = ""
        self._debug_branch_rows = {}
        self._debug_final_rows = {}
        if self.debug_stream_file.exists():
            self.debug_stream_file.unlink()

    def _debug_policy_specs(self) -> list[tuple[str, int, bool]]:
        return [
            ("rounds1_free", 1, False),
            ("rounds2_free", 2, False),
            ("rounds3_free", 3, False),
            ("rounds2_pause", 2, True),
        ]

    def _debug_policy_state(self, name: str) -> dict[str, Any]:
        if name not in self._debug_policy_states:
            self._debug_policy_states[name] = {
                "accepted_tokens": [],
                "candidate_tokens": [],
                "candidate_count": 0,
                "alignment_lost": False,
            }
        return self._debug_policy_states[name]

    def _short_debug_text(self, text: str, limit: int = 120) -> str:
        text = normalize_text(text)
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    def _update_debug_policy_streams(
        self,
        candidate_tokens: list[str],
        current_tokens: list[str],
    ) -> None:
        if not self.session_debug_streams:
            return

        for name, stable_rounds, pause_on_diverge in self._debug_policy_specs():
            state = self._debug_policy_state(name)
            accepted_tokens = list(state["accepted_tokens"])

            if pause_on_diverge and accepted_tokens and not state["alignment_lost"]:
                common = common_prefix_len_fuzzy(accepted_tokens, current_tokens)
                if common < len(accepted_tokens):
                    state["alignment_lost"] = True

            if candidate_tokens == state["candidate_tokens"]:
                state["candidate_count"] += 1
            else:
                state["candidate_tokens"] = list(candidate_tokens)
                state["candidate_count"] = 1

            if (
                not state["alignment_lost"]
                and state["candidate_count"] >= stable_rounds
                and len(candidate_tokens) > len(accepted_tokens)
            ):
                state["accepted_tokens"] = list(candidate_tokens)

    def _format_debug_policy_board(self) -> list[str]:
        lines = ["simulated_accept_streams:"]
        for name, stable_rounds, pause_on_diverge in self._debug_policy_specs():
            state = self._debug_policy_state(name)
            accepted = detokenize(state["accepted_tokens"])
            candidate = detokenize(state["candidate_tokens"])
            pause = " pause_on_diverge" if pause_on_diverge else ""
            lines.append(self._preview_row(name, f"stable_rounds={stable_rounds}{pause} rounds={state['candidate_count']} paused={state['alignment_lost']}"))
            lines.append(self._preview_row(f"{name}.accept", accepted or "<empty>"))
            lines.append(self._preview_row(f"{name}.stable", candidate or "<empty>"))
        return lines

    def _format_debug_snapshot_board(self) -> list[str]:
        lines = ["rolling_transcript_window:"]
        snapshots = list(self._debug_stream_snapshots)
        for idx in range(DEBUG_SNAPSHOT_ROWS):
            if idx < len(snapshots):
                snap = snapshots[idx]
                meta = (
                    f"t+{snap['t']}s tokens={snap['tokens']} "
                    f"common={snap['common']} stable={snap['stable_tokens']}"
                )
                current = snap["current"] or "<empty>"
                stable = snap["stable"] or "<empty>"
            else:
                meta = ""
                current = ""
                stable = ""
            lines.append(self._preview_row(f"win{idx + 1:02d}.meta", meta))
            lines.append(self._preview_row(f"win{idx + 1:02d}.current", current))
            lines.append(self._preview_row(f"win{idx + 1:02d}.stable", stable))
        return lines

    def _format_branch_expression(self, branches: list[tuple[str, list[str]]]) -> str:
        if not any(tokens for _label, tokens in branches):
            return ""

        prefix_len = common_prefix_len_many([tokens for _label, tokens in branches])
        first_tokens = next(tokens for _label, tokens in branches if tokens)
        prefix = detokenize(first_tokens[:prefix_len])
        parts: list[str] = []
        for label, tokens in branches:
            if not tokens:
                suffix = "<empty>"
            else:
                suffix = detokenize(tokens[prefix_len:]) or "<same>"
            parts.append(f"{label}:{suffix}")

        branch = " | ".join(parts)
        if prefix:
            return f"{prefix} ({branch})"
        return f"({branch})"

    def _format_debug_streams(self) -> list[str]:
        live_commit_enabled = self.live_commit and not (
            self.session_debug_streams or self.last_debug_streams
        )
        lines = [
            "debug_streams",
            self._preview_row("debug_active", str(self.session_debug_streams)),
            self._preview_row("live_commit", str(live_commit_enabled)),
            self._preview_row("alignment_lost", str(self._live_alignment_lost)),
            self._preview_row(
                "debug_params",
                f"window_seconds={self.window_seconds} "
                f"min_window_seconds={self.min_window_seconds} "
                f"chunk_ms={self.chunk_ms} "
                f"stable_rounds={self.stable_rounds} "
                f"live_strategy={self.live_strategy} "
                f"beam_size={self.config.model.beam_size} "
                f"vad_filter={self.config.model.vad_filter} "
                f"condition_on_previous_text={self.config.model.condition_on_previous_text}",
            ),
            self._preview_row("branch.common", self._debug_branch_rows.get("common", "")),
            self._preview_row("branch.accept", self._debug_branch_rows.get("would_accept", "")),
            self._preview_row("branch.stable", self._debug_branch_rows.get("stable", "")),
            self._preview_row("branch.prev", self._debug_branch_rows.get("prev", "")),
            self._preview_row("branch.current", self._debug_branch_rows.get("current", "")),
            self._preview_row("final.accept", self._debug_final_rows.get("would_accept", "")),
            self._preview_row("final.text", self._debug_final_rows.get("final", "")),
        ]
        lines.extend(self._format_debug_policy_board())
        lines.extend(self._format_debug_snapshot_board())
        lines.append("recent_events:")
        events = list(self._debug_stream_events)
        for idx in range(DEBUG_EVENT_ROWS):
            event = events[idx] if idx < len(events) else ""
            lines.append(self._preview_row(f"event{idx + 1:02d}", event))
        return lines

    def _write_debug_streams_file(self) -> None:
        if self.session_active:
            committed = detokenize(self._live_committed_tokens)
            tail = detokenize(self._live_prev_tokens[len(self._live_committed_tokens) :])
            self._write_preview(committed, tail, note="recording")

    def _record_live_debug_profile(
        self,
        profile: dict[str, Any],
        audio_s: float,
        snapshot_s: float,
        decode_s: float,
        process_s: float,
    ) -> None:
        if not self.session_debug_streams:
            return

        branch = str(profile.get("branch_expression", ""))
        if branch:
            self._debug_branch_expression = branch

        event = (
            f"live t+{max(0.0, time.time() - self.session_started_at):.2f}s "
            f"audio_s={audio_s:.2f} snapshot_s={snapshot_s:.3f} "
            f"decode_s={decode_s:.3f} process_s={process_s:.3f} "
            f"tokens={profile.get('tokens', 0)} "
            f"would_accept={profile.get('committed_tokens', 0)} "
            f"stable={profile.get('candidate_tokens', 0)} "
            f"prev_common={profile.get('prev_current_common_tokens', 0)} "
            f"would_accept_common={profile.get('accepted_current_common_tokens', 0)} "
            f"rounds={profile.get('candidate_count', 0)}/{self.stable_rounds} "
            f"delta_chars={profile.get('delta_chars', 0)} "
            f"paused={profile.get('alignment_lost', False)} "
            f"reset={profile.get('reset', False)}"
        )
        self._debug_stream_events.append(event)
        self._write_debug_streams_file()
        self.logger.info("Live debug branch %s", branch)

    def _write_pid(self) -> None:
        self.config.paths.pid_file.write_text(str(os.getpid()), encoding="utf-8")

    def _cleanup_pid(self) -> None:
        if self.config.paths.pid_file.exists():
            self.config.paths.pid_file.unlink()

    def _set_last_error(self, error: str) -> None:
        self.last_error = error
        self.activity_state = "error"
        self.activity_updated_at = time.time()
        self.logger.error(error)
        self._persist_status()

    def _set_activity(self, state: str, persist: bool = True) -> None:
        self.activity_state = state
        self.activity_updated_at = time.time()
        self.sound_player.play(state)
        if persist:
            self._persist_status()

    def _apply_profile(self, profile: ProfileConfig) -> None:
        if profile.default_mode:
            self.mode = profile.default_mode
        if profile.chunk_ms is not None:
            self.chunk_ms = profile.chunk_ms
        if profile.window_seconds is not None:
            self.window_seconds = profile.window_seconds
        if profile.min_window_seconds is not None:
            self.min_window_seconds = profile.min_window_seconds
        if profile.stable_rounds is not None:
            self.stable_rounds = profile.stable_rounds
        if profile.live_commit is not None:
            self.live_commit = profile.live_commit
        if profile.live_strategy is not None:
            if profile.live_strategy not in {"prefix", "rolling_append", "draft_replace", "mutable_tail"}:
                raise ValueError(f"unknown live_strategy: {profile.live_strategy}")
            self.live_strategy = profile.live_strategy
        if profile.inject_backends is not None:
            self.inject_backends = list(profile.inject_backends)
        if profile.append_trailing_space is not None:
            self.append_trailing_space = profile.append_trailing_space

        self.ring_max_bytes = max(1, int(self.window_seconds * self.bytes_per_second))
        self.min_window_bytes = max(1, int(self.min_window_seconds * self.bytes_per_second))

    def _simulate_divergence(
        self,
        *,
        live_text: str,
        final_text: str,
        confirm_delete: bool,
        backends: list[str] | None,
        append_trailing_space: Any = UNSET,
    ) -> dict[str, Any]:
        live_text = normalize_text(live_text)
        final_text = normalize_text(final_text)
        if self.session_active or self.external_session_active:
            return {"ok": False, "error": "cannot simulate divergence while a session is active"}
        if not live_text:
            return {"ok": False, "error": "live text is required"}
        if not final_text:
            return {"ok": False, "error": "final text is required"}
        if not confirm_delete:
            return {
                "ok": False,
                "error": "refusing to backspace without --confirm-delete",
            }

        saved_committed_tokens = list(self._live_committed_tokens)
        saved_candidate_tokens = list(self._live_candidate_tokens)
        saved_pending_append_tokens = list(self._live_pending_append_tokens)
        saved_prev_tokens = list(self._live_prev_tokens)
        saved_append_window_tokens = list(self._live_append_window_tokens)
        saved_injected_text = self._live_injected_text
        saved_candidate_count = self._live_candidate_count
        saved_alignment_lost = self._live_alignment_lost
        saved_last_injected_at = self._live_last_injected_at
        saved_session_backends = self.session_inject_backends
        saved_append_trailing_space = self.append_trailing_space

        if backends is not None:
            self.session_inject_backends = list(backends)
        if append_trailing_space is not UNSET:
            self.append_trailing_space = bool(append_trailing_space)

        action = ""
        injected_text = ""
        backend = ""
        message = ""
        injection_ok = False
        prefix_len = 0
        live_payload = ""

        try:
            self._live_committed_tokens = []
            self._live_candidate_tokens = []
            self._live_pending_append_tokens = []
            self._live_prev_tokens = []
            self._live_append_window_tokens = []
            self._live_injected_text = ""
            self._live_candidate_count = 0
            self._live_alignment_lost = False
            self._live_last_injected_at = 0.0

            live_result = self._inject_text(live_text)
            if not live_result.get("ok"):
                message = str(live_result.get("message") or "live injection failed")
                self._set_last_result(
                    "injection_failed",
                    reason=message,
                    text=final_text,
                    injected_text=str(live_result.get("payload") or ""),
                    backend=str(live_result.get("backend") or ""),
                    message=message,
                    injection_ok=False,
                    details={
                        "stage": "live_inject",
                        "live_tokens": len(tokenize(live_text)),
                        "final_tokens": len(tokenize(final_text)),
                    },
                )
                self._persist_status()
                return {
                    "ok": False,
                    "error": message,
                    "stage": "live_inject",
                    "backend": str(live_result.get("backend") or ""),
                }

            live_payload = str(live_result.get("payload") or "")
            self._live_injected_text = live_payload
            self._live_committed_tokens = tokenize(live_text)
            self._live_last_injected_at = time.monotonic()

            final_tokens = tokenize(final_text)
            committed_tokens = list(self._live_committed_tokens)
            prefix_len = common_prefix_len_fuzzy(committed_tokens, final_tokens)
            if prefix_len == len(committed_tokens):
                action = "append_remainder"
                remainder_tokens = final_tokens[prefix_len:]
            else:
                removal_ok = self._remove_live_injected_text()
                if removal_ok:
                    action = "replace_live_with_final"
                    remainder_tokens = final_tokens
                else:
                    action = "skip_removal_failed"
                    remainder_tokens = []

            remainder_text = detokenize(remainder_tokens)
            final_result: dict[str, Any] | None = None
            if remainder_text:
                final_result = self._inject_text(remainder_text)

            if action == "skip_removal_failed":
                message = "live text removal failed; final injection skipped"
                injection_ok = False
            elif final_result is not None and not final_result.get("ok"):
                message = str(final_result.get("message") or "final injection failed")
                injected_text = str(final_result.get("payload") or "")
                backend = str(final_result.get("backend") or "")
                injection_ok = False
            else:
                message = str(final_result.get("message") or "") if final_result else ""
                injected_text = str(final_result.get("payload") or "")
                backend = str(final_result.get("backend") or "") if final_result else ""
                injection_ok = True

            result_kind = "committed" if injection_ok else "injection_failed"
            self._set_last_result(
                result_kind,
                reason=message or action,
                text=final_text,
                injected_text=injected_text,
                backend=backend,
                message=message,
                injection_ok=injection_ok,
                details={
                    "action": action,
                    "live_tokens": len(committed_tokens),
                    "final_tokens": len(final_tokens),
                    "common_tokens": prefix_len,
                    "live_payload_chars": len(live_payload),
                    "remainder_tokens": len(remainder_tokens),
                },
            )
            self._set_activity("committed", persist=False)
            self._write_preview(final_text, "", note=f"simulated {action}")
            self._persist_status()

            if not injection_ok:
                return {
                    "ok": False,
                    "error": message,
                    "action": action,
                    "backend": backend,
                    "text": final_text,
                }
            return {
                "ok": True,
                "message": "simulated divergence",
                "action": action,
                "backend": backend,
                "text": final_text,
            }
        finally:
            self._live_committed_tokens = saved_committed_tokens
            self._live_candidate_tokens = saved_candidate_tokens
            self._live_pending_append_tokens = saved_pending_append_tokens
            self._live_prev_tokens = saved_prev_tokens
            self._live_append_window_tokens = saved_append_window_tokens
            self._live_injected_text = saved_injected_text
            self._live_candidate_count = saved_candidate_count
            self._live_alignment_lost = saved_alignment_lost
            self._live_last_injected_at = saved_last_injected_at
            self.session_inject_backends = saved_session_backends
            self.append_trailing_space = saved_append_trailing_space

    def _switch_profile(self, profile_name: str) -> dict[str, Any]:
        if not profile_name:
            return {"ok": False, "error": "profile name is required"}
        if self.session_active:
            return {"ok": False, "error": "cannot switch profile while recording"}

        profile = load_profile(self.config.profiles_dir, profile_name)
        self.profile_name = profile_name
        self.profile = profile
        self._apply_profile(profile)
        self._set_activity("idle", persist=False)
        self._persist_status()
        self._write_preview("", "", note="profile switched")
        return {
            "ok": True,
            "message": f"profile switched to {profile_name}",
            "mode": self.mode,
            "inject_backends": self.inject_backends,
        }

    def _toggle_live(self) -> dict[str, Any]:
        new_mode = "live" if self.mode == "ptt" else "ptt"
        return self._set_mode(new_mode)

    def _set_mode(self, mode: str) -> dict[str, Any]:
        if self.session_active:
            return {"ok": False, "error": "cannot change mode while recording"}
        self.mode = mode
        self._set_activity("idle", persist=False)
        self._write_preview("", "", note=f"mode switched to {mode}")
        self._persist_status()
        return {"ok": True, "mode": self.mode}

    def _set_input_target(self, target: str | None) -> dict[str, Any]:
        if self.session_active:
            return {"ok": False, "error": "cannot change input target while recording"}
        self.input_target = target
        if target:
            self.input_target_file.write_text(target, encoding="utf-8")
        elif self.input_target_file.exists():
            self.input_target_file.unlink()
        self._set_activity("idle", persist=False)
        note = (
            f"input target set to '{target}'"
            if target
            else "input target cleared (default source will be used)"
        )
        self._write_preview("", "", note=note)
        self._persist_status()
        return {"ok": True, "message": note, "input_target": self.input_target}

    def _session_text_backends_for_target(self, target: str) -> list[str] | None:
        _ = target
        return None

    def _session_uses_inline_markers(self) -> bool:
        return False

    def _clear_session_marker_context(self) -> None:
        self.session_marker_target = "auto"
        self.session_kitty_context = {}
        self.session_inject_backends = None
        self.session_inline_markers = False
        self.injector.clear_kitty_context()

    def _load_model(self) -> None:
        if self.config.model.cuda_lib_hints:
            self._apply_cuda_library_hints()

        attempts: list[tuple[str, str]] = [(self.config.model.device, self.config.model.compute_type)]
        if self.config.model.device == "cuda":
            attempts.extend([("cuda", "int8_float16"), ("cpu", "int8")])
        elif ("cpu", "int8") not in attempts:
            attempts.append(("cpu", "int8"))

        errors: list[str] = []
        for device, compute_type in attempts:
            try:
                self.logger.info(
                    "Loading model '%s' on %s / %s",
                    self.config.model.name,
                    device,
                    compute_type,
                )
                model = WhisperModel(self.config.model.name, device=device, compute_type=compute_type)
                self.model = model
                self.active_model_device = device
                self.active_model_compute_type = compute_type
                self.last_error = ""
                self.logger.info("Model loaded on %s / %s", device, compute_type)
                return
            except Exception as exc:
                err = f"{device}/{compute_type}: {exc}"
                errors.append(err)
                self.logger.warning("Model init failed on %s", err)

        raise RuntimeError("All model init attempts failed: " + " | ".join(errors))

    @staticmethod
    def _is_cuda_runtime_failure(exc: Exception) -> bool:
        text = str(exc).lower()
        needles = (
            "cuda failed",
            "libcublas",
            "libcudnn",
            "cublas",
            "cudnn",
            "operation not supported on this os",
        )
        return any(needle in text for needle in needles)

    def _fallback_model_to_cpu(self) -> None:
        if self.active_model_device == "cpu":
            return
        self.logger.warning("Falling back model to CPU int8 after CUDA runtime failure")
        self.model = WhisperModel(self.config.model.name, device="cpu", compute_type="int8")
        self.active_model_device = "cpu"
        self.active_model_compute_type = "int8"
        self.last_error = ""
        self._persist_status()

    def _apply_cuda_library_hints(self) -> None:
        # Helps CTranslate2 find pip-installed CUDA libs in virtualenvs.
        candidates: list[Path] = []
        for site_dir in site.getsitepackages():
            base = Path(site_dir)
            candidates.append(base / "nvidia/cublas/lib")
            candidates.append(base / "nvidia/cudnn/lib")

        existing = [str(path) for path in candidates if path.exists()]
        if not existing:
            return

        current = os.environ.get("LD_LIBRARY_PATH", "")
        merged = ":".join(existing + ([current] if current else []))
        os.environ["LD_LIBRARY_PATH"] = merged

    def _start_session(
        self,
        requested_input_target: Any = UNSET,
        marker_target: str = "auto",
        kitty_context: dict[str, str] | None = None,
        debug_streams: bool = False,
    ) -> dict[str, Any]:
        if self.session_active:
            return {"ok": False, "error": "session already active"}
        if self.external_session_active:
            return {
                "ok": False,
                "error": f"external session already active: {self.external_session_id}",
            }
        _ = kitty_context

        self.display_session_counter += 1
        self.display_session_id = f"local-{self.display_session_counter}"
        active_target = (
            self.input_target if requested_input_target is UNSET else requested_input_target
        )
        self.session_input_target = active_target
        self.session_marker_target = parse_marker_target(marker_target)
        self.session_kitty_context = {}
        self.session_inject_backends = None
        self.session_inline_markers = False
        self.session_debug_streams = debug_streams
        self.last_debug_streams = debug_streams
        self._reset_debug_streams()
        self.injector.clear_kitty_context()
        capture_cmd = with_capture_target(self.config.audio.capture_command, active_target)

        self.session_audio = bytearray()
        self.ring_chunks = deque()
        self.ring_total_bytes = 0
        self.audio_level_percent = 0.0
        self.session_peak_level_percent = 0.0
        self.session_voice_seen = False
        self.session_last_voice_seen_at = 0.0
        self.session_voice_chunk_count = 0
        self.session_strong_voice_chunk_count = 0

        self._live_prev_tokens = []
        self._live_candidate_tokens = []
        self._live_candidate_count = 0
        self._live_committed_tokens = []
        self._live_injected_text = ""
        self._live_append_window_tokens = []
        self._live_pending_append_tokens = []
        self._live_alignment_lost = False
        self._live_input_marker_visible = False
        self._live_input_marker_text = ""
        self._live_input_marker_state = "idle"
        self._live_last_injected_at = 0.0
        self._live_level_marker_updated_at = 0.0

        self.session_stop_event = threading.Event()
        self.capture = AudioRecorder(
            self.config.audio,
            self.logger,
            self._on_audio_chunk,
            capture_command=capture_cmd,
        )

        try:
            self.capture.start()
        except Exception as exc:
            self._set_last_error(f"Failed to start audio capture: {exc}")
            self.session_debug_streams = False
            self._clear_session_marker_context()
            return {"ok": False, "error": str(exc)}

        self.session_active = True
        self.session_started_at = time.time()
        self.last_error = ""
        self._set_activity("recording", persist=False)

        if (
            self.mode == "live"
            and self._session_live_commit_enabled()
            and self._session_uses_inline_markers()
        ):
            self._insert_live_input_marker(self._live_marker_text("recording"), "recording")
            self.marker_thread = threading.Thread(
                target=self._marker_loop,
                name="marker-loop",
                daemon=True,
            )
            self.marker_thread.start()

        if self.mode == "live":
            self.live_thread = threading.Thread(target=self._live_loop, name="live-loop", daemon=True)
            self.live_thread.start()

        self._write_preview("", "", note="recording")
        self._write_debug_streams_file()
        self._persist_status()
        self.logger.info(
            "Session started (mode=%s, profile=%s, input_target=%s, "
            "inject_backends=%s, inline_markers=%s, debug_streams=%s)",
            self.mode,
            self.profile_name,
            active_target or "<default>",
            self.inject_backends,
            False,
            self.session_debug_streams,
        )

        return {
            "ok": True,
            "message": "recording started",
            "mode": self.mode,
            "profile": self.profile_name,
            "input_target": active_target,
            "inject_backends": self.inject_backends,
            "inline_markers": False,
            "debug_streams": self.session_debug_streams,
        }

    def _stop_session(self, commit: bool) -> dict[str, Any]:
        if not self.session_active:
            return {"ok": False, "error": "no active session"}

        stop_started = time.perf_counter()
        assert self.session_stop_event is not None
        self.session_stop_event.set()

        capture_stop_s = 0.0
        if self.capture:
            capture_stop_s = self.capture.stop()
            self.capture = None

        if self.live_thread:
            self.live_thread.join(timeout=2.0)
            self.live_thread = None

        if self.marker_thread:
            self.marker_thread.join(timeout=1.0)
            self.marker_thread = None

        session_audio = bytes(self.session_audio)
        self.session_active = False
        self.session_input_target = None
        if commit:
            self._set_activity("finalizing")
        elapsed = max(0.0, time.time() - self.session_started_at)

        if not commit:
            self._remove_live_input_marker()
            self.session_audio = bytearray()
            self.ring_chunks = deque()
            self.ring_total_bytes = 0
            self.session_debug_streams = False
            self._set_activity("cancelled", persist=False)
            self._set_last_result(
                "cancelled",
                reason="session cancelled before commit",
                elapsed_s=elapsed,
            )
            self._write_preview("", "", note="cancelled")
            self._write_debug_streams_file()
            self._clear_session_marker_context()
            self._persist_status()
            self.logger.info(
                "Session cancelled after %.2fs (capture_stop_s=%.3f total_stop_s=%.3f)",
                elapsed,
                capture_stop_s,
                time.perf_counter() - stop_started,
            )
            return {"ok": True, "message": "cancelled", "elapsed_s": round(elapsed, 3)}

        if not self._session_voice_ready():
            self._remove_live_input_marker()
            self.session_audio = bytearray()
            self.ring_chunks = deque()
            self.ring_total_bytes = 0
            self.session_debug_streams = False
            self._set_activity("no_voice", persist=False)
            self._set_last_result(
                "no_voice",
                reason=(
                    f"peak {self.session_peak_level_percent:.2f}% below gate; "
                    f"voice_chunks={self.session_voice_chunk_count}/{AUDIO_LEVEL_READY_MIN_CHUNKS}; "
                    f"strong_chunks={self.session_strong_voice_chunk_count}/"
                    f"{AUDIO_LEVEL_READY_MIN_STRONG_CHUNKS}"
                ),
                elapsed_s=elapsed,
                details={
                    "peak_level_percent": round(self.session_peak_level_percent, 2),
                    "threshold_percent": AUDIO_LEVEL_THRESHOLD_PERCENT,
                    "strong_threshold_percent": AUDIO_LEVEL_STRONG_THRESHOLD_PERCENT,
                    "voice_chunk_count": self.session_voice_chunk_count,
                    "voice_chunk_required": AUDIO_LEVEL_READY_MIN_CHUNKS,
                    "strong_voice_chunk_count": self.session_strong_voice_chunk_count,
                    "strong_voice_chunk_required": AUDIO_LEVEL_READY_MIN_STRONG_CHUNKS,
                },
            )
            self._write_preview("", "", note="low volume / no voice")
            self._write_debug_streams_file()
            self._clear_session_marker_context()
            self._persist_status()
            self.logger.info(
                "Session ignored after %.2fs: peak_level=%.2f%% "
                "voice_chunks=%d/%d strong_chunks=%d/%d thresholds=%.2f%%/%.2f%%",
                elapsed,
                self.session_peak_level_percent,
                self.session_voice_chunk_count,
                AUDIO_LEVEL_READY_MIN_CHUNKS,
                self.session_strong_voice_chunk_count,
                AUDIO_LEVEL_READY_MIN_STRONG_CHUNKS,
                AUDIO_LEVEL_THRESHOLD_PERCENT,
                AUDIO_LEVEL_STRONG_THRESHOLD_PERCENT,
            )
            return {
                "ok": True,
                "message": "low volume / no voice",
                "elapsed_s": round(elapsed, 3),
                "peak_level_percent": round(self.session_peak_level_percent, 2),
                "threshold_percent": AUDIO_LEVEL_THRESHOLD_PERCENT,
                "strong_threshold_percent": AUDIO_LEVEL_STRONG_THRESHOLD_PERCENT,
                "voice_chunk_count": self.session_voice_chunk_count,
                "strong_voice_chunk_count": self.session_strong_voice_chunk_count,
            }

        if not session_audio:
            self._remove_live_input_marker()
            self.session_debug_streams = False
            self._set_activity("no_audio", persist=False)
            self._set_last_result(
                "no_audio",
                reason="no audio captured",
                elapsed_s=elapsed,
            )
            self._write_preview("", "", note="no audio captured")
            self._write_debug_streams_file()
            self._clear_session_marker_context()
            self._persist_status()
            return {"ok": True, "message": "no audio captured", "elapsed_s": round(elapsed, 3)}

        try:
            final_transcribe_started = time.perf_counter()
            final_text, _ = self._transcribe_pcm(session_audio)
            final_transcribe_s = time.perf_counter() - final_transcribe_started
        except Exception as exc:
            self._remove_live_input_marker()
            self._set_activity("error", persist=False)
            self._set_last_result(
                "transcription_error",
                reason=f"final transcription failed: {exc}",
                elapsed_s=elapsed,
            )
            self._clear_session_marker_context()
            raise
        final_tokens = tokenize(final_text)
        final_action = "inject_remainder"
        final_skip_reason = ""
        prefix_len = 0

        if (
            self.mode == "live"
            and self._session_live_commit_enabled()
            and self.live_strategy == "mutable_tail"
        ):
            committed_tokens = list(self._live_committed_tokens)
            live_text_removal_ok = self._remove_live_injected_text()
            if live_text_removal_ok:
                prefix_len = (
                    common_prefix_len_fuzzy(committed_tokens, final_tokens)
                    if committed_tokens
                    else 0
                )
                if not committed_tokens:
                    final_action = "full_final"
                    remainder_tokens = final_tokens
                elif prefix_len == len(committed_tokens):
                    final_action = "append_remainder"
                    remainder_tokens = final_tokens[prefix_len:]
                else:
                    self.logger.warning(
                        "Mutable-tail/final divergence detected: committed=%d final=%d common=%d; "
                        "skipping final repair because stable accepted text is already in the field",
                        len(committed_tokens),
                        len(final_tokens),
                        prefix_len,
                    )
                    final_action = "skip_divergence"
                    final_skip_reason = (
                        "mutable-tail final diverged from stable accepted text; "
                        "final repair skipped to avoid duplicate text"
                    )
                    remainder_tokens = []
            else:
                self.logger.warning(
                    "Mutable-tail final commit skipped because mutable tail removal failed"
                )
                final_action = "skip_removal_failed"
                final_skip_reason = "mutable tail removal failed before final injection"
                remainder_tokens = []
        elif (
            self.mode == "live"
            and self._session_live_commit_enabled()
            and self.live_strategy == "draft_replace"
        ):
            committed_tokens = list(self._live_committed_tokens)
            live_text_removal_ok = self._remove_live_injected_text()
            if live_text_removal_ok:
                if committed_tokens:
                    prefix_len = common_prefix_len_fuzzy(committed_tokens, final_tokens)
                    self.logger.info(
                        "Draft-replace final commit replacing draft=%d final=%d common=%d",
                        len(committed_tokens),
                        len(final_tokens),
                        prefix_len,
                    )
                remainder_tokens = final_tokens
            else:
                self.logger.warning(
                    "Draft-replace final commit skipped because live draft removal failed"
                )
                final_action = "skip_removal_failed"
                final_skip_reason = "live draft removal failed before final injection"
                remainder_tokens = []
        elif (
            self.mode == "live"
            and self._session_live_commit_enabled()
            and self.live_strategy == "rolling_append"
        ):
            committed_tokens = list(self._live_committed_tokens)
            prefix_len = common_prefix_len_fuzzy(committed_tokens, final_tokens) if committed_tokens else 0
            live_text_removal_ok = True
            if committed_tokens and prefix_len < len(committed_tokens):
                live_text_removal_ok = self._remove_live_injected_text()
            remainder_tokens, final_action, prefix_len = rolling_append_final_remainder(
                committed_tokens,
                final_tokens,
                live_text_removal_ok=live_text_removal_ok,
            )
            if final_action == "replace_live_with_final":
                self.logger.warning(
                    "Rolling-append/final divergence detected: committed=%d final=%d common=%d; "
                    "replacing live text with final transcript",
                    len(committed_tokens),
                    len(final_tokens),
                    prefix_len,
                )
                final_skip_reason = "rolling append diverged; replacing live text with final transcript"
            elif final_action == "skip_removal_failed":
                self.logger.warning(
                    "Rolling-append/final divergence detected: committed=%d final=%d common=%d; "
                    "skipping final injection because live-text removal failed",
                    len(committed_tokens),
                    len(final_tokens),
                    prefix_len,
                )
                final_skip_reason = "live text removal failed; final injection skipped"
        elif self.mode == "live" and self._session_live_commit_enabled():
            committed_tokens = list(self._live_committed_tokens)
            if committed_tokens:
                prefix_len = common_prefix_len_fuzzy(committed_tokens, final_tokens)
            else:
                prefix_len = 0

            if not committed_tokens or prefix_len == len(committed_tokens):
                remainder_tokens = final_tokens[prefix_len:]
            else:
                live_text_removal_ok = self._remove_live_injected_text()
                if live_text_removal_ok:
                    self.logger.warning(
                        "Live/final divergence detected: committed=%d final=%d common=%d; "
                        "replacing live text with final transcript",
                        len(committed_tokens),
                        len(final_tokens),
                        prefix_len,
                    )
                    final_action = "replace_live_with_final"
                    final_skip_reason = "live/final divergence; replaced live text with final transcript"
                    remainder_tokens = final_tokens
                else:
                    self.logger.warning(
                        "Live/final divergence detected: committed=%d final=%d common=%d; "
                        "skipping final injection because live-text removal failed",
                        len(committed_tokens),
                        len(final_tokens),
                        prefix_len,
                    )
                    final_action = "skip_removal_failed"
                    final_skip_reason = "live/final divergence; live-text removal failed"
                    remainder_tokens = []
        else:
            remainder_tokens = final_tokens

        remainder_text = detokenize(remainder_tokens)
        if self.session_debug_streams or self.last_debug_streams:
            accepted_tokens = list(self._live_committed_tokens)
            self._debug_final_rows = {
                "would_accept": detokenize(accepted_tokens),
                "final": detokenize(final_tokens),
            }
            self._debug_final_expression = self._format_branch_expression(
                [
                    ("would_accept", accepted_tokens),
                    ("final", final_tokens),
                ]
            )
            self._debug_stream_events.append(
                "final "
                f"decode_s={final_transcribe_s:.3f} "
                f"would_accept_tokens={len(accepted_tokens)} "
                f"final_tokens={len(final_tokens)} "
                f"remainder_tokens={len(remainder_tokens)}"
            )
            self._write_debug_streams_file()
        self._remove_live_input_marker()
        injection_result: dict[str, Any] | None = None
        if remainder_text:
            injection_result = self._inject_text(remainder_text)

        self.last_commit_text = final_text
        if injection_result is None or injection_result.get("ok"):
            self.last_error = ""
        self.config.paths.last_commit_file.write_text(final_text, encoding="utf-8")

        self.session_debug_streams = False
        self._set_activity("committed", persist=False)
        if final_action in {"skip_divergence", "skip_removal_failed"} and not remainder_text:
            result_kind = "divergence_skip"
            result_reason = final_skip_reason or "final injection skipped"
            injection_ok = False
            backend = ""
            message = result_reason
            injected_text = ""
        elif injection_result is not None and not injection_result.get("ok"):
            result_kind = "injection_failed"
            result_reason = str(injection_result.get("message") or "final injection failed")
            injection_ok = False
            backend = str(injection_result.get("backend") or "")
            message = result_reason
            injected_text = str(injection_result.get("payload") or "")
        else:
            result_kind = "committed"
            result_reason = (
                "final text injected"
                if remainder_text
                else "final text already covered by live injection"
            )
            injection_ok = True
            backend = str(injection_result.get("backend") or "") if injection_result else ""
            message = str(injection_result.get("message") or "") if injection_result else ""
            injected_text = str(injection_result.get("payload") or "")
        self._set_last_result(
            result_kind,
            reason=result_reason,
            text=final_text,
            injected_text=injected_text,
            backend=backend,
            message=message,
            injection_ok=injection_ok,
            elapsed_s=elapsed,
            details={
                "final_action": final_action,
                "final_tokens": len(final_tokens),
                "remainder_tokens": len(remainder_tokens),
                "committed_tokens": len(self._live_committed_tokens),
                "common_tokens": prefix_len,
                "live_strategy": self.live_strategy,
            },
        )
        self._write_preview(final_text, "", note="committed")
        self._write_debug_streams_file()
        self._clear_session_marker_context()
        self._persist_status()
        self.logger.info(
            "Session committed after %.2fs (capture_stop_s=%.3f final_transcribe_s=%.3f "
            "total_stop_s=%.3f text_chars=%d)",
            elapsed,
            capture_stop_s,
            final_transcribe_s,
            time.perf_counter() - stop_started,
            len(final_text),
        )

        return {
            "ok": True,
            "message": "committed",
            "elapsed_s": round(elapsed, 3),
            "text": final_text,
            "injected_remainder": remainder_text,
            "mode": self.mode,
        }

    def _inject_text_command(
        self,
        text: str,
        backends: list[str] | None = None,
        append_trailing_space: bool | None = None,
    ) -> dict[str, Any]:
        result = self._inject_text(
            text,
            backends=backends,
            append_trailing_space=append_trailing_space,
        )
        response = {
            "ok": result["ok"],
            "message": result["message"],
            "backend": result["backend"],
            "payload": result["payload"],
            "backends": result["backends"],
        }
        if not result["ok"]:
            response["error"] = result["message"]
        self._persist_status()
        return response

    def _audio_level_bucket(self) -> int:
        max_bucket = len(LIVE_VOLUME_BLOCKS) - 1
        display_max = max(AUDIO_LEVEL_DISPLAY_MAX_PERCENT, AUDIO_LEVEL_THRESHOLD_PERCENT)
        scaled_level = min(1.0, max(0.0, self.audio_level_percent) / display_max)
        return max(0, min(max_bucket, int(round(scaled_level * max_bucket))))

    def _session_voice_ready(self) -> bool:
        return (
            self.session_voice_chunk_count >= AUDIO_LEVEL_READY_MIN_CHUNKS
            and self.session_strong_voice_chunk_count >= AUDIO_LEVEL_READY_MIN_STRONG_CHUNKS
        )

    def _audio_gate_label(self) -> str:
        if not self.session_active:
            return "idle"
        if self._session_voice_ready():
            return "accepting voice"
        return "rejecting silence"

    def _live_marker_text(self, state: str) -> str:
        marker = LIVE_MARKERS[state]
        if state in {"recording", "decoding", "active"}:
            return f"{marker}{LIVE_VOLUME_BLOCKS[self._audio_level_bucket()]}"
        return marker

    def _marker_backends(self) -> list[str]:
        if not self._session_uses_inline_markers():
            return []

        system_backends = [
            backend
            for backend in self.inject_backends
            if backend in MARKER_CAPABLE_BACKENDS and backend != "kitty"
        ]
        target = self.session_marker_target

        backends: list[str] = []
        if target in {"auto", "kitty"} and self.injector.available_backends().get("kitty"):
            backends.append("kitty")
        if target in {"auto", "system"} or not backends:
            backends.extend(backend for backend in system_backends if backend not in backends)
        return backends

    def _update_live_level_marker(self) -> None:
        with self.marker_lock:
            if not self._live_input_marker_visible:
                return
            if self._live_input_marker_state not in {"recording", "decoding", "active"}:
                return

            now = time.monotonic()
            if now - self._live_level_marker_updated_at < LIVE_LEVEL_MARKER_UPDATE_SECONDS:
                return

            marker = self._live_marker_text(self._live_input_marker_state)
            if marker == self._live_input_marker_text:
                return

            self._live_level_marker_updated_at = now
            self._rewrite_live_input_marker_suffix(
                marker,
                self._live_input_marker_state,
                quiet=True,
            )

    def _marker_loop(self) -> None:
        assert self.session_stop_event is not None
        interval = max(0.04, min(0.08, LIVE_LEVEL_MARKER_UPDATE_SECONDS / 2.0))
        while not self.session_stop_event.is_set():
            time.sleep(interval)
            if self.session_active:
                self._update_live_level_marker()

    def _insert_live_input_marker(self, marker: str, state: str, quiet: bool = False) -> bool:
        with self.marker_lock:
            if not self._session_uses_inline_markers():
                return True
            if (
                self._live_input_marker_visible
                and self._live_input_marker_text == marker
                and self._live_input_marker_state == state
            ):
                return True

            if self._live_input_marker_visible and not self._remove_live_input_marker(quiet=quiet):
                return False

            result = self._inject_text(
                marker,
                backends=self._marker_backends(),
                append_trailing_space=False,
                quiet=quiet,
            )
            if result["ok"]:
                self._live_input_marker_visible = True
                self._live_input_marker_text = marker
                self._live_input_marker_state = state
                self._live_level_marker_updated_at = time.monotonic()
                if not quiet:
                    self.logger.info("Live input marker inserted state=%s marker=%s", state, marker)
                return True

            self.logger.warning(
                "Live input marker insert failed state=%s marker=%s: %s",
                state,
                marker,
                result["message"],
            )
            return False

    def _remove_live_input_marker(self, quiet: bool = False) -> bool:
        with self.marker_lock:
            if not self._live_input_marker_visible:
                return True

            marker = self._live_input_marker_text
            state = self._live_input_marker_state
            started = time.perf_counter()
            ok, backend, message = self.injector.backspace(self._marker_backends(), count=len(marker))
            elapsed_s = time.perf_counter() - started
            if ok:
                self._live_input_marker_visible = False
                self._live_input_marker_text = ""
                self._live_input_marker_state = "idle"
                self.last_injected_backend = backend
                self.last_injection_message = message
                self.last_error = ""
                if not quiet:
                    self.logger.info(
                        "Live input marker removed state=%s marker=%s backend=%s elapsed_s=%.3f",
                        state,
                        marker,
                        backend,
                        elapsed_s,
                    )
                return True

            self.last_injection_message = message
            self._set_last_error(f"Live input marker removal failed: {message}")
            return False

    def _replace_live_input_marker(self, marker: str, state: str, quiet: bool = False) -> bool:
        with self.marker_lock:
            if not self._session_uses_inline_markers():
                if self._live_input_marker_visible:
                    return self._remove_live_input_marker(quiet=quiet)
                return True
            if self._live_input_marker_visible and not self._remove_live_input_marker(quiet=quiet):
                return False
            return self._insert_live_input_marker(marker, state, quiet=quiet)

    def _set_live_input_marker_state(self, state: str, quiet: bool = True) -> bool:
        if not self._session_uses_inline_markers():
            return True
        marker = self._live_marker_text(state)
        with self.marker_lock:
            if not self._live_input_marker_visible:
                return self._insert_live_input_marker(marker, state, quiet=quiet)
            if self._live_input_marker_state == state:
                return self._rewrite_live_input_marker_suffix(marker, state, quiet=quiet)
            return self._replace_live_input_marker(marker, state, quiet=quiet)

    def _rewrite_live_input_marker_suffix(
        self,
        marker: str,
        state: str,
        quiet: bool = False,
    ) -> bool:
        with self.marker_lock:
            if not self._live_input_marker_visible:
                return self._insert_live_input_marker(marker, state, quiet=quiet)
            if self._live_input_marker_state != state:
                return self._replace_live_input_marker(marker, state, quiet=quiet)

            old_marker = self._live_input_marker_text
            prefix_len = common_prefix_char_len(old_marker, marker)
            backspace_count = len(old_marker) - prefix_len
            suffix = marker[prefix_len:]
            if backspace_count == 0 and not suffix:
                return True

            if backspace_count > 0:
                ok, backend, message = self.injector.backspace(
                    self._marker_backends(),
                    count=backspace_count,
                )
                if not ok:
                    self.last_injection_message = message
                    self._set_last_error(f"Live input marker suffix removal failed: {message}")
                    return False
                self.last_injected_backend = backend
                self.last_injection_message = message
                self._live_input_marker_text = old_marker[:prefix_len]

            if suffix:
                result = self._inject_text(
                    suffix,
                    backends=self._marker_backends(),
                    append_trailing_space=False,
                    quiet=quiet,
                )
                if not result["ok"]:
                    return False

            self._live_input_marker_visible = True
            self._live_input_marker_text = marker
            self._live_input_marker_state = state
            return True

    def _flash_live_input_marker(
        self,
        marker: str,
        state: str,
        duration_s: float = LIVE_MARKER_FLASH_SECONDS,
    ) -> bool:
        if not self._session_uses_inline_markers():
            return True
        if not self._replace_live_input_marker(marker, state):
            return False
        time.sleep(duration_s)
        return self._remove_live_input_marker()

    def _inject_live_delta(self, text: str) -> dict[str, Any]:
        total_started = time.perf_counter()
        with self.marker_lock:
            marker_was_visible = self._live_input_marker_visible
            remove_s = 0.0
            insert_s = 0.0
            if marker_was_visible and not self._remove_live_input_marker():
                return {
                    "ok": False,
                    "backend": "none",
                    "message": self.last_error,
                    "payload": text,
                    "backends": list(self.inject_backends),
                }
            if marker_was_visible:
                remove_s = time.perf_counter() - total_started

            result = self._inject_text(text)
            inject_done = time.perf_counter()
            if marker_was_visible or result["ok"]:
                self._insert_live_input_marker(self._live_marker_text("active"), "active")
                insert_s = time.perf_counter() - inject_done
            total_s = time.perf_counter() - total_started
        result["live_marker_remove_s"] = remove_s
        result["live_marker_insert_s"] = insert_s
        result["live_total_s"] = total_s
        self.logger.info(
            "Live injection profile delta_chars=%d remove_s=%.3f inject_s=%.3f "
            "insert_s=%.3f total_s=%.3f ok=%s",
            len(text),
            remove_s,
            float(result.get("elapsed_s", 0.0)),
            insert_s,
            total_s,
            result["ok"],
        )
        return result

    def _inject_text(
        self,
        text: str,
        backends: list[str] | None = None,
        append_trailing_space: bool | None = None,
        quiet: bool = False,
    ) -> dict[str, Any]:
        payload = normalize_text(text)
        default_backends = (
            self.session_inject_backends
            if self.session_inject_backends is not None
            else self.inject_backends
        )
        if not payload:
            return {
                "ok": True,
                "backend": "none",
                "message": "nothing to inject",
                "payload": "",
                "backends": list(backends) if backends is not None else list(default_backends),
            }

        should_append = self.append_trailing_space
        if append_trailing_space is not None:
            should_append = append_trailing_space
        if should_append and not payload.endswith((" ", "\n", "\t")):
            payload += " "

        active_backends = list(backends) if backends is not None else list(default_backends)
        inject_started = time.perf_counter()
        ok, backend, message = self.injector.inject(payload, active_backends)
        inject_s = time.perf_counter() - inject_started
        if ok:
            self.last_injected_backend = backend
            self.last_injection_message = message
            self.last_error = ""
            if not quiet:
                self.logger.info(
                    "Injected text with backend=%s elapsed_s=%.3f chars=%d",
                    backend,
                    inject_s,
                    len(payload),
                )
            return {
                "ok": True,
                "backend": backend,
                "message": message,
                "payload": payload,
                "backends": active_backends,
                "elapsed_s": inject_s,
            }
        else:
            error = f"Injection failed: {message}"
            self.last_injection_message = message
            self._set_last_error(error)
            return {
                "ok": False,
                "backend": "none",
                "message": error,
                "payload": payload,
                "backends": active_backends,
                "elapsed_s": inject_s,
            }

    def _remove_live_injected_text(self) -> bool:
        if not self._live_injected_text:
            return True

        active_backends = (
            self.session_inject_backends
            if self.session_inject_backends is not None
            else self.inject_backends
        )
        ok, backend, message = self.injector.backspace(
            list(active_backends),
            count=len(self._live_injected_text),
        )
        if ok:
            self.last_injected_backend = backend
            self.last_injection_message = message
            self.logger.info(
                "Removed live-injected text with backend=%s chars=%d",
                backend,
                len(self._live_injected_text),
            )
            self._live_injected_text = ""
            return True

        self.last_injection_message = message
        self.logger.warning(
            "Failed to remove live-injected text before final repair: %s",
            message,
        )
        return False

    def _replace_live_draft_text(self, text: str) -> dict[str, Any]:
        if not self._remove_live_injected_text():
            return {
                "ok": False,
                "backend": "none",
                "message": self.last_injection_message or "failed to remove live draft",
                "payload": text,
                "backends": list(self.inject_backends),
                "elapsed_s": 0.0,
            }

        result = self._inject_text(text)
        if result["ok"]:
            self._live_injected_text = str(result.get("payload", ""))
            self.logger.info(
                "Replaced live draft chars=%d",
                len(self._live_injected_text),
            )
        return result

    def _on_audio_chunk(self, data: bytes) -> None:
        if not data:
            return
        if not self.session_active:
            return
        level_percent = pcm_rms_percent(data)
        self.audio_level_percent = level_percent
        self.session_peak_level_percent = max(self.session_peak_level_percent, level_percent)
        now = time.monotonic()
        was_voice_ready = self.session_voice_seen
        if level_percent >= AUDIO_LEVEL_THRESHOLD_PERCENT:
            self.session_voice_chunk_count += 1
            self.session_last_voice_seen_at = now
        if level_percent >= AUDIO_LEVEL_STRONG_THRESHOLD_PERCENT:
            self.session_strong_voice_chunk_count += 1
        self.session_voice_seen = self._session_voice_ready()
        if self.session_voice_seen and not was_voice_ready:
            self.sound_player.play("voice_ready")
            self.logger.info(
                "Voice gate accepted: peak_level=%.2f%% voice_chunks=%d/%d "
                "strong_chunks=%d/%d",
                self.session_peak_level_percent,
                self.session_voice_chunk_count,
                AUDIO_LEVEL_READY_MIN_CHUNKS,
                self.session_strong_voice_chunk_count,
                AUDIO_LEVEL_READY_MIN_STRONG_CHUNKS,
            )

        self.session_audio.extend(data)
        self.ring_chunks.append(data)
        self.ring_total_bytes += len(data)

        while self.ring_total_bytes > self.ring_max_bytes and self.ring_chunks:
            removed = self.ring_chunks.popleft()
            self.ring_total_bytes -= len(removed)

    def _ring_snapshot(self) -> bytes:
        if not self.ring_chunks:
            return b""
        return b"".join(self.ring_chunks)

    def _live_loop(self) -> None:
        assert self.session_stop_event is not None
        interval = max(0.1, self.chunk_ms / 1000.0)

        while not self.session_stop_event.is_set():
            time.sleep(interval)
            if not self.session_active:
                continue

            snapshot_started = time.perf_counter()
            audio = self._ring_snapshot()
            snapshot_s = time.perf_counter() - snapshot_started
            if len(audio) < self.min_window_bytes:
                continue

            if not self._should_live_decode():
                if self._live_input_marker_state == "decoding":
                    next_state = "active" if self._live_committed_tokens else "recording"
                    self._set_live_input_marker_state(next_state)
                continue

            try:
                self._set_live_input_marker_state("decoding")
                decode_started = time.perf_counter()
                text, _ = self._transcribe_pcm(audio)
                decode_s = time.perf_counter() - decode_started
                process_started = time.perf_counter()
                profile = self._process_live_partial(text)
                process_s = time.perf_counter() - process_started
                audio_s = len(audio) / float(self.bytes_per_second)
                self._record_live_debug_profile(profile, audio_s, snapshot_s, decode_s, process_s)
                if self._live_input_marker_state == "decoding" and self._live_committed_tokens:
                    self._set_live_input_marker_state("active")
                if profile["delta_chars"] > 0 or decode_s >= 0.5:
                    self.logger.info(
                        "Live decode profile audio_s=%.2f snapshot_s=%.3f "
                        "decode_s=%.3f process_s=%.3f tokens=%d committed=%d "
                        "delta_chars=%d",
                        audio_s,
                        snapshot_s,
                        decode_s,
                        process_s,
                        profile["tokens"],
                        profile["committed_tokens"],
                        profile["delta_chars"],
                    )
            except Exception as exc:
                self._set_last_error(f"Live decode failed: {exc}")

    def _should_live_decode(self) -> bool:
        if not self._session_voice_ready():
            return False
        return time.monotonic() - self.session_last_voice_seen_at <= AUDIO_LEVEL_GRACE_SECONDS

    def _process_live_partial(self, partial_text: str) -> dict[str, Any]:
        if self.live_strategy == "draft_replace":
            return self._process_live_partial_draft_replace(partial_text)
        if self.live_strategy == "mutable_tail":
            return self._process_live_partial_mutable_tail(partial_text)
        if self.live_strategy == "rolling_append":
            return self._process_live_partial_rolling_append(partial_text)
        return self._process_live_partial_prefix(partial_text)

    def _process_live_partial_prefix(self, partial_text: str) -> dict[str, Any]:
        if not self._session_voice_ready():
            return {"tokens": 0, "committed_tokens": len(self._live_committed_tokens), "delta_chars": 0}
        current_tokens = tokenize(partial_text)
        if not current_tokens:
            return {"tokens": 0, "committed_tokens": len(self._live_committed_tokens), "delta_chars": 0}

        prev_tokens = list(self._live_prev_tokens)
        accepted_before_reset = list(self._live_committed_tokens)
        accepted_common = common_prefix_len_fuzzy(accepted_before_reset, current_tokens)
        reset = self._maybe_reset_live_window(current_tokens)
        prefix_len = common_prefix_len(self._live_prev_tokens, current_tokens)
        candidate_tokens = current_tokens[:prefix_len]

        if candidate_tokens == self._live_candidate_tokens:
            self._live_candidate_count += 1
        else:
            self._live_candidate_tokens = candidate_tokens
            self._live_candidate_count = 1

        delta_text = ""
        if (
            not self._live_alignment_lost
            and self._live_candidate_count >= self.stable_rounds
            and len(candidate_tokens) > len(self._live_committed_tokens)
        ):
            delta_tokens = candidate_tokens[len(self._live_committed_tokens) :]
            delta_text = detokenize(delta_tokens)
            if self._session_live_commit_enabled() and delta_text:
                result = self._inject_live_delta(delta_text)
                if result["ok"]:
                    self._live_injected_text += str(result.get("payload", ""))
                    self._live_committed_tokens = list(candidate_tokens)
                    self._live_last_injected_at = time.monotonic()
            else:
                self._live_committed_tokens = list(candidate_tokens)
                if delta_text:
                    self._live_last_injected_at = time.monotonic()

        self._update_debug_policy_streams(candidate_tokens, current_tokens)

        committed_text = detokenize(self._live_committed_tokens)
        tail_tokens = current_tokens[len(self._live_committed_tokens) :]
        tail_text = detokenize(tail_tokens)
        self._live_prev_tokens = current_tokens

        branch_expression = ""
        if self.session_debug_streams:
            common_tokens = []
            branches = [
                ("would_accept", self._live_committed_tokens),
                ("stable", candidate_tokens),
                ("prev", prev_tokens),
                ("current", current_tokens),
            ]
            if any(tokens for _label, tokens in branches):
                prefix_len_many = common_prefix_len_many([tokens for _label, tokens in branches])
                first_tokens = next(tokens for _label, tokens in branches if tokens)
                common_tokens = first_tokens[:prefix_len_many]
            self._debug_branch_rows = {
                "common": detokenize(common_tokens),
                "would_accept": detokenize(self._live_committed_tokens),
                "stable": detokenize(candidate_tokens),
                "prev": detokenize(prev_tokens),
                "current": detokenize(current_tokens),
            }
            branch_expression = self._format_branch_expression(
                branches
            )
            self._debug_branch_expression = branch_expression
            self._debug_stream_snapshots.append(
                {
                    "t": f"{max(0.0, time.time() - self.session_started_at):.2f}",
                    "tokens": str(len(current_tokens)),
                    "common": str(prefix_len),
                    "stable_tokens": str(len(candidate_tokens)),
                    "current": self._short_debug_text(detokenize(current_tokens), limit=160),
                    "stable": self._short_debug_text(detokenize(candidate_tokens), limit=160),
                }
            )

        self._write_preview(committed_text, tail_text, note="recording")
        return {
            "tokens": len(current_tokens),
            "committed_tokens": len(self._live_committed_tokens),
            "candidate_tokens": len(candidate_tokens),
            "candidate_count": self._live_candidate_count,
            "prev_current_common_tokens": prefix_len,
            "accepted_current_common_tokens": accepted_common,
            "delta_chars": len(delta_text),
            "alignment_lost": self._live_alignment_lost,
            "reset": reset,
            "branch_expression": branch_expression,
        }

    def _process_live_partial_rolling_append(self, partial_text: str) -> dict[str, Any]:
        if not self._session_voice_ready():
            return {"tokens": 0, "committed_tokens": len(self._live_committed_tokens), "delta_chars": 0}
        current_tokens = tokenize(partial_text)
        if not current_tokens:
            return {"tokens": 0, "committed_tokens": len(self._live_committed_tokens), "delta_chars": 0}

        prev_tokens = list(self._live_prev_tokens)
        committed_before = list(self._live_committed_tokens)
        committed_tail = committed_before[-ROLLING_APPEND_COMMITTED_TAIL_TOKENS:]
        committed_overlap_len = suffix_prefix_overlap_len_fuzzy(committed_tail, current_tokens)
        prev_overlap_len = suffix_prefix_overlap_len_fuzzy(prev_tokens, current_tokens)
        overlap_len = committed_overlap_len or prev_overlap_len
        align_source = "none"

        if not committed_before and not prev_tokens:
            candidate_tokens = current_tokens
            align_source = "initial"
        elif committed_overlap_len >= ROLLING_APPEND_MIN_OVERLAP_TOKENS:
            candidate_tokens = current_tokens[committed_overlap_len:]
            align_source = "committed_tail"
        elif not committed_before and prev_overlap_len >= ROLLING_APPEND_MIN_OVERLAP_TOKENS:
            candidate_tokens = current_tokens[prev_overlap_len:]
            align_source = "prev_window"
        else:
            candidate_tokens = []
            align_source = "unaligned"

        if candidate_tokens:
            candidate_tokens = candidate_tokens[:-1] if len(candidate_tokens) > 1 else []
            candidate_tokens = trim_prefix_already_in_suffix(committed_before, candidate_tokens)

        stable_prefix_len = (
            common_prefix_len_fuzzy(self._live_pending_append_tokens, candidate_tokens)
            if self._live_pending_append_tokens and candidate_tokens
            else 0
        )
        if stable_prefix_len > 0:
            self._live_candidate_count += 1
        else:
            self._live_pending_append_tokens = list(candidate_tokens)
            self._live_candidate_count = 1 if candidate_tokens else 0

        delta_text = ""
        appended_tokens: list[str] = []
        if self._live_candidate_count >= self.stable_rounds and stable_prefix_len > 0:
            appended_tokens = list(self._live_pending_append_tokens[:stable_prefix_len])
            appended_tokens = trim_prefix_already_in_suffix(committed_before, appended_tokens)
            delta_text = detokenize(appended_tokens)
            if self._session_live_commit_enabled() and delta_text:
                result = self._inject_live_delta(delta_text)
                if result["ok"]:
                    self._live_injected_text += str(result.get("payload", ""))
                    self._live_committed_tokens.extend(appended_tokens)
                    self._live_last_injected_at = time.monotonic()
            else:
                self._live_committed_tokens.extend(appended_tokens)
                if delta_text:
                    self._live_last_injected_at = time.monotonic()
            remaining_tokens = candidate_tokens[stable_prefix_len:]
            self._live_pending_append_tokens = trim_prefix_already_in_suffix(
                self._live_committed_tokens,
                remaining_tokens,
            )
            self._live_candidate_count = 1 if self._live_pending_append_tokens else 0

        self._live_append_window_tokens = current_tokens
        self._update_debug_policy_streams(self._live_committed_tokens, current_tokens)

        committed_text = detokenize(self._live_committed_tokens)
        tail_text = detokenize(current_tokens[-min(len(current_tokens), 16) :])
        self._live_prev_tokens = current_tokens

        branch_expression = ""
        if self.session_debug_streams:
            branches = [
                ("would_accept", self._live_committed_tokens),
                ("append", appended_tokens or candidate_tokens),
                ("prev", prev_tokens),
                ("current", current_tokens),
            ]
            self._debug_branch_rows = {
                "common": (
                    f"align={align_source} committed_overlap={committed_overlap_len} "
                    f"prev_overlap={prev_overlap_len}"
                ),
                "would_accept": detokenize(self._live_committed_tokens),
                "stable": detokenize(appended_tokens or candidate_tokens),
                "prev": detokenize(prev_tokens),
                "current": detokenize(current_tokens),
            }
            branch_expression = self._format_branch_expression(branches)
            self._debug_branch_expression = branch_expression
            self._debug_stream_snapshots.append(
                {
                    "t": f"{max(0.0, time.time() - self.session_started_at):.2f}",
                    "tokens": str(len(current_tokens)),
                    "common": f"{align_source}:{overlap_len}",
                    "stable_tokens": str(len(candidate_tokens)),
                    "current": self._short_debug_text(detokenize(current_tokens), limit=160),
                    "stable": self._short_debug_text(detokenize(candidate_tokens), limit=160),
                }
            )

        self._write_preview(committed_text, tail_text, note="recording")
        return {
            "tokens": len(current_tokens),
            "committed_tokens": len(self._live_committed_tokens),
            "candidate_tokens": len(candidate_tokens),
            "candidate_count": self._live_candidate_count,
            "prev_current_common_tokens": overlap_len,
            "accepted_current_common_tokens": committed_overlap_len,
            "delta_chars": len(delta_text),
            "alignment_lost": False,
            "reset": False,
            "branch_expression": branch_expression,
        }

    def _process_live_partial_mutable_tail(self, partial_text: str) -> dict[str, Any]:
        if not self._session_voice_ready():
            return {"tokens": 0, "committed_tokens": len(self._live_committed_tokens), "delta_chars": 0}
        current_tokens = tokenize(partial_text)
        if not current_tokens:
            return {"tokens": 0, "committed_tokens": len(self._live_committed_tokens), "delta_chars": 0}

        prev_tokens = list(self._live_prev_tokens)
        committed_before = list(self._live_committed_tokens)
        committed_tail = committed_before[-ROLLING_APPEND_COMMITTED_TAIL_TOKENS:]
        committed_overlap_len = suffix_prefix_overlap_len_fuzzy(committed_tail, current_tokens)
        prev_overlap_len = suffix_prefix_overlap_len_fuzzy(prev_tokens, current_tokens)
        align_source = "none"

        if not committed_before and not self._live_prev_tokens:
            candidate_tokens = current_tokens
            align_source = "initial"
        elif committed_overlap_len >= ROLLING_APPEND_MIN_OVERLAP_TOKENS:
            candidate_tokens = current_tokens[committed_overlap_len:]
            align_source = "committed_tail"
        elif not committed_before and prev_overlap_len >= ROLLING_APPEND_MIN_OVERLAP_TOKENS:
            candidate_tokens = current_tokens[prev_overlap_len:]
            align_source = "prev_window"
        else:
            candidate_tokens = []
            align_source = "unaligned"

        accepted_tokens: list[str] = []
        mutable_tokens: list[str] = []
        if candidate_tokens:
            if len(candidate_tokens) > 2:
                accepted_tokens = candidate_tokens[:-2]
                mutable_tokens = candidate_tokens[-2:]
            else:
                mutable_tokens = candidate_tokens
            accepted_tokens = trim_prefix_already_in_suffix(committed_before, accepted_tokens)

        if accepted_tokens == self._live_pending_append_tokens:
            self._live_candidate_count += 1
        else:
            self._live_pending_append_tokens = list(accepted_tokens)
            self._live_candidate_count = 1

        delta_text = ""
        if self._live_candidate_count >= self.stable_rounds and accepted_tokens:
            if self._session_live_commit_enabled():
                if self._remove_live_injected_text():
                    delta_text = detokenize(accepted_tokens)
                    result = self._inject_live_delta(delta_text)
                    if result["ok"]:
                        self._live_committed_tokens.extend(accepted_tokens)
                        self._live_last_injected_at = time.monotonic()
                        if mutable_tokens:
                            draft_result = self._inject_text(detokenize(mutable_tokens))
                            if draft_result["ok"]:
                                self._live_injected_text = str(draft_result.get("payload", ""))
                            else:
                                self._live_injected_text = ""
                else:
                    mutable_tokens = []
            else:
                delta_text = detokenize(accepted_tokens)
                self._live_committed_tokens.extend(accepted_tokens)
                if delta_text:
                    self._live_last_injected_at = time.monotonic()
            self._live_pending_append_tokens = []
            self._live_candidate_count = 0
        elif self._session_live_commit_enabled():
            mutable_text = detokenize(mutable_tokens)
            previous_mutable = normalize_text(self._live_injected_text)
            if mutable_text and mutable_text != previous_mutable:
                result = self._replace_live_draft_text(mutable_text)
                if result["ok"] and mutable_text:
                    self._live_last_injected_at = time.monotonic()
            elif not mutable_text and self._live_injected_text:
                self._remove_live_injected_text()

        self._live_prev_tokens = current_tokens
        self._live_append_window_tokens = current_tokens
        self._update_debug_policy_streams(self._live_committed_tokens, current_tokens)

        committed_text = detokenize(self._live_committed_tokens)
        tail_text = detokenize(mutable_tokens)
        overlap_len = committed_overlap_len or prev_overlap_len

        branch_expression = ""
        if self.session_debug_streams:
            branches = [
                ("accepted", self._live_committed_tokens),
                ("mutable", mutable_tokens),
                ("current", current_tokens),
            ]
            self._debug_branch_rows = {
                "common": (
                    f"mutable_tail align={align_source} committed_overlap={committed_overlap_len} "
                    f"prev_overlap={prev_overlap_len}"
                ),
                "would_accept": detokenize(self._live_committed_tokens),
                "stable": detokenize(accepted_tokens),
                "prev": detokenize(prev_tokens),
                "current": detokenize(current_tokens),
            }
            branch_expression = self._format_branch_expression(branches)
            self._debug_branch_expression = branch_expression

        self._write_preview(committed_text, tail_text, note="recording")
        return {
            "tokens": len(current_tokens),
            "committed_tokens": len(self._live_committed_tokens),
            "candidate_tokens": len(accepted_tokens),
            "candidate_count": self._live_candidate_count,
            "prev_current_common_tokens": overlap_len,
            "accepted_current_common_tokens": committed_overlap_len,
            "delta_chars": len(delta_text),
            "alignment_lost": align_source == "unaligned",
            "reset": False,
            "branch_expression": branch_expression,
        }

    def _process_live_partial_draft_replace(self, partial_text: str) -> dict[str, Any]:
        if not self._session_voice_ready():
            return {"tokens": 0, "committed_tokens": len(self._live_committed_tokens), "delta_chars": 0}
        current_tokens = tokenize(partial_text)
        if not current_tokens:
            return {"tokens": 0, "committed_tokens": len(self._live_committed_tokens), "delta_chars": 0}

        draft_tokens = current_tokens[:-1] if len(current_tokens) > 1 else current_tokens
        draft_text = detokenize(draft_tokens)
        previous_text = detokenize(self._live_committed_tokens)
        delta_text = ""

        if draft_text != previous_text:
            delta_text = draft_text
            if self._session_live_commit_enabled():
                result = self._replace_live_draft_text(draft_text)
                if result["ok"]:
                    self._live_committed_tokens = list(draft_tokens)
                    if draft_text:
                        self._live_last_injected_at = time.monotonic()
            else:
                self._live_committed_tokens = list(draft_tokens)
                if draft_text:
                    self._live_last_injected_at = time.monotonic()

        self._live_prev_tokens = current_tokens
        committed_text = detokenize(self._live_committed_tokens)
        tail_text = detokenize(current_tokens[len(draft_tokens) :])

        branch_expression = ""
        if self.session_debug_streams:
            branches = [
                ("draft", self._live_committed_tokens),
                ("current", current_tokens),
            ]
            self._debug_branch_rows = {
                "common": "draft_replace",
                "would_accept": detokenize(self._live_committed_tokens),
                "stable": detokenize(draft_tokens),
                "prev": previous_text,
                "current": detokenize(current_tokens),
            }
            branch_expression = self._format_branch_expression(branches)
            self._debug_branch_expression = branch_expression
            self._debug_stream_snapshots.append(
                {
                    "t": f"{max(0.0, time.time() - self.session_started_at):.2f}",
                    "tokens": str(len(current_tokens)),
                    "common": "draft",
                    "stable_tokens": str(len(draft_tokens)),
                    "current": self._short_debug_text(detokenize(current_tokens), limit=160),
                    "stable": self._short_debug_text(draft_text, limit=160),
                }
            )

        self._write_preview(committed_text, tail_text, note="recording")
        return {
            "tokens": len(current_tokens),
            "committed_tokens": len(self._live_committed_tokens),
            "candidate_tokens": len(draft_tokens),
            "candidate_count": 1,
            "prev_current_common_tokens": 0,
            "accepted_current_common_tokens": 0,
            "delta_chars": len(delta_text),
            "alignment_lost": False,
            "reset": False,
            "branch_expression": branch_expression,
        }

    def _maybe_reset_live_window(self, current_tokens: list[str]) -> bool:
        if self._live_alignment_lost:
            return False
        if not self._live_committed_tokens or not current_tokens:
            return False

        prefix_len = common_prefix_len_fuzzy(self._live_committed_tokens, current_tokens)
        if prefix_len == len(self._live_committed_tokens):
            return False

        if self._live_last_injected_at <= 0.0:
            return False

        since_last_injection = time.monotonic() - self._live_last_injected_at
        reset_after_s = max(2.0, self.window_seconds * 0.5)
        if since_last_injection < reset_after_s:
            return False

        self.logger.info(
            "Pausing live rolling-window injection after %.2fs divergence "
            "(committed=%d current=%d common=%d)",
            since_last_injection,
            len(self._live_committed_tokens),
            len(current_tokens),
            prefix_len,
        )
        self._live_alignment_lost = True
        self._live_candidate_tokens = []
        self._live_candidate_count = 0
        return True

    def _transcribe_pcm(self, pcm_bytes: bytes) -> tuple[str, dict[str, Any]]:
        if self.model is None:
            raise RuntimeError("Model is not loaded")
        if len(pcm_bytes) < 2:
            return "", {}

        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        kwargs: dict[str, Any] = {
            "beam_size": self.config.model.beam_size,
            "vad_filter": self.config.model.vad_filter,
            "condition_on_previous_text": self.config.model.condition_on_previous_text,
        }
        if self.config.model.language:
            kwargs["language"] = self.config.model.language

        with self.model_lock:
            try:
                segments, info = self.model.transcribe(audio, **kwargs)
            except Exception as exc:
                if self.active_model_device == "cuda" and self._is_cuda_runtime_failure(exc):
                    self.logger.warning("CUDA runtime error during transcribe: %s", exc)
                    self._fallback_model_to_cpu()
                    assert self.model is not None
                    segments, info = self.model.transcribe(audio, **kwargs)
                else:
                    raise
            text = normalize_text(" ".join(seg.text.strip() for seg in segments))

        info_payload = {
            "language": getattr(info, "language", None),
            "language_probability": getattr(info, "language_probability", None),
        }
        return text, info_payload


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        daemon: DictationDaemon = self.server.daemon_ref  # type: ignore[attr-defined]
        raw = self.rfile.readline().decode("utf-8", errors="replace").strip()
        if not raw:
            response = {"ok": False, "error": "empty request"}
        else:
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                response = {"ok": False, "error": "invalid json"}
            else:
                response = daemon.handle_command(request)

        self.wfile.write((json.dumps(response, ensure_ascii=True) + "\n").encode("utf-8"))


def send_control_command(socket_path: Path, payload: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        line = json.dumps(payload, ensure_ascii=True).encode("utf-8") + b"\n"
        client.sendall(line)

        chunks: list[bytes] = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break

    response_raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not response_raw:
        raise RuntimeError("empty response from daemon")
    return json.loads(response_raw)


def run_smoke(config: AppConfig, audio_path: Path) -> int:
    audio_path = audio_path.resolve()
    if not audio_path.exists():
        print(f"Audio file not found: {audio_path}")
        return 1

    daemon = DictationDaemon(config)
    try:
        daemon._load_model()
    except Exception as exc:
        print(f"Model load failed: {exc}")
        return 1

    start = time.perf_counter()
    pcm = read_audio_with_ffmpeg(audio_path, config.audio.sample_rate, config.audio.channels)
    text, info = daemon._transcribe_pcm(pcm)
    elapsed = time.perf_counter() - start

    duration_s = len(pcm) / float(config.audio.sample_rate * config.audio.channels * 2)
    rtf = elapsed / duration_s if duration_s > 0 else 0.0

    print(f"audio: {audio_path}")
    print(f"duration_s: {duration_s:.3f}")
    print(f"elapsed_s: {elapsed:.3f}")
    print(f"rtf: {rtf:.4f}")
    print(f"model_device: {daemon.active_model_device}")
    print(f"model_compute_type: {daemon.active_model_compute_type}")
    print(f"language: {info.get('language')} prob={info.get('language_probability')}")
    print(f"text: {text}")
    return 0


def read_audio_with_ffmpeg(path: Path, sample_rate: int, channels: int) -> bytes:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required for smoke test")

    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(path),
        "-f",
        "s16le",
        "-ac",
        str(channels),
        "-ar",
        str(sample_rate),
        "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip())
    return proc.stdout


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local dictation daemon using faster-whisper")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    daemon_parser = sub.add_parser("daemon", help="Run dictation daemon")
    daemon_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    smoke_parser = sub.add_parser("smoke", help="Run one-shot local transcription smoke test")
    smoke_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    smoke_parser.add_argument("--audio", type=Path, default=Path(__file__).resolve().parent / "jfk.wav")

    ctl_parser = sub.add_parser("ctl", help="Send control command directly")
    ctl_parser.add_argument("cmd", type=str)
    ctl_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    ctl_parser.add_argument("--mode", type=str)
    ctl_parser.add_argument("--profile", type=str)
    ctl_parser.add_argument("--input-target", type=str)
    ctl_parser.add_argument(
        "--marker-target",
        choices=["auto", "kitty", "system"],
        help=argparse.SUPPRESS,
    )
    ctl_parser.add_argument("--text", type=str)
    ctl_parser.add_argument(
        "--backend",
        dest="backends",
        action="append",
        default=None,
        help="Injector backend override (repeatable).",
    )
    ctl_parser.add_argument(
        "--no-trailing-space",
        action="store_true",
        help="Disable trailing space for inject_text command.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.subcommand == "daemon":
        config = load_config(args.config)
        daemon = DictationDaemon(config)

        def handle_signal(_: int, __: Any) -> None:
            daemon.shutdown_event.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        return daemon.run()

    if args.subcommand == "smoke":
        config = load_config(args.config)
        return run_smoke(config, args.audio)

    if args.subcommand == "ctl":
        config = load_config(args.config)
        payload: dict[str, Any] = {"cmd": args.cmd}
        if args.mode:
            payload["mode"] = args.mode
        if args.profile:
            payload["profile"] = args.profile
        if args.input_target is not None:
            payload["input_target"] = args.input_target
        if args.marker_target is not None:
            payload["marker_target"] = args.marker_target
            if args.marker_target == "kitty":
                payload["kitty_context"] = {
                    key: os.environ[key] for key in KITTY_CONTEXT_KEYS if os.environ.get(key)
                }
        if args.text is not None:
            payload["text"] = args.text
        if args.backends:
            payload["backends"] = args.backends
        if args.no_trailing_space:
            payload["append_trailing_space"] = False
        response = send_control_command(config.paths.socket_path, payload)
        print(json.dumps(response, indent=2))
        return 0 if response.get("ok") else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
