#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tomllib
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"
KITTY_CONTEXT_KEYS = ("KITTY_LISTEN_ON", "KITTY_WINDOW_ID", "KITTY_PUBLIC_KEY")


def load_socket_path(config_path: Path) -> Path:
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    paths_cfg = config.get("paths", {})
    raw = paths_cfg.get("socket_path", "state/dictation.sock")
    path = Path(raw)
    if path.is_absolute():
        return path
    return (config_path.parent / path).resolve()


def send_command(socket_path: Path, payload: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall((json.dumps(payload) + "\n").encode("utf-8"))

        chunks: list[bytes] = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break

    raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not raw:
        raise RuntimeError("empty response from daemon")
    return json.loads(raw)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Control local dictation daemon")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--json", action="store_true", help="Print raw JSON response")

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    start = sub.add_parser("start")
    start.add_argument(
        "--input-target",
        dest="input_target",
        default=None,
        help="PipeWire input target name/ID to pass to pw-record --target",
    )
    start.add_argument(
        "--marker-target",
        choices=["auto", "kitty", "system"],
        default=None,
        help=argparse.SUPPRESS,
    )
    start.add_argument(
        "--debug-streams",
        action="store_true",
        help="Show live/final branch diagnostics and suppress live insertion for this session",
    )
    sub.add_parser("stop-commit")
    sub.add_parser("stop-cancel")
    sub.add_parser("toggle-live")

    set_mode = sub.add_parser("set-mode")
    set_mode.add_argument("mode", choices=["ptt", "live"])

    set_input_target = sub.add_parser("set-input-target")
    set_input_target.add_argument(
        "input_target",
        nargs="?",
        default="",
        help="Set persistent input target. Omit value to clear.",
    )

    switch_profile = sub.add_parser("switch-profile")
    switch_profile.add_argument("profile")

    inject_text = sub.add_parser("inject-text")
    inject_text.add_argument("text", help="Text to inject into focused field")
    inject_text.add_argument(
        "--backend",
        dest="backends",
        action="append",
        default=[],
        help="Override backend list for this command (repeatable)",
    )
    inject_text.add_argument(
        "--no-trailing-space",
        action="store_true",
        help="Do not append trailing space to injected text",
    )

    simulate_divergence = sub.add_parser(
        "simulate-divergence",
        help="Inject live text, then simulate final/live divergence replacement",
    )
    simulate_divergence.add_argument("--live", required=True, help="Text to inject as the live draft")
    simulate_divergence.add_argument("--final", required=True, help="Final transcript to reconcile against")
    simulate_divergence.add_argument(
        "--confirm-delete",
        action="store_true",
        help="Required: allow the daemon to backspace the simulated live text",
    )
    simulate_divergence.add_argument(
        "--backend",
        dest="backends",
        action="append",
        default=[],
        help="Override backend list for this command (repeatable)",
    )
    simulate_divergence.add_argument(
        "--no-trailing-space",
        action="store_true",
        help="Do not append trailing space to injected text",
    )

    safe_test = sub.add_parser("safe-test")
    safe_test.add_argument(
        "text",
        nargs="?",
        default="safe clipboard test",
        help="Text to copy to clipboard without typing into focused field",
    )

    external_event = sub.add_parser("external-event")
    external_event.add_argument("type", choices=["start", "partial", "final", "cancel", "error", "end"])
    external_event.add_argument("--session-id", required=True)
    external_event.add_argument("--source", default="android")
    external_event.add_argument("--seq", type=int, default=None)
    external_event.add_argument("--text", default="")
    external_event.add_argument("--error", default="")
    external_event.add_argument(
        "--no-trailing-space",
        action="store_true",
        help="Do not append trailing space for final text injection",
    )

    sub.add_parser("tail")
    sub.add_parser("shutdown")

    return parser.parse_args()


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    mapping = {
        "stop-commit": "stop_commit",
        "stop-cancel": "stop_cancel",
        "toggle-live": "toggle_live",
        "set-mode": "set_mode",
        "set-input-target": "set_input_target",
        "switch-profile": "switch_profile",
        "inject-text": "inject_text",
        "simulate-divergence": "simulate_divergence",
        "safe-test": "inject_text",
        "external-event": "external_event",
    }
    cmd = mapping.get(args.cmd, args.cmd)

    payload: dict[str, Any] = {"cmd": cmd}
    if args.cmd == "start" and args.input_target is not None:
        payload["input_target"] = args.input_target
    if args.cmd == "start" and args.marker_target is not None:
        payload["marker_target"] = args.marker_target
        if args.marker_target == "kitty":
            payload["kitty_context"] = {
                key: os.environ[key] for key in KITTY_CONTEXT_KEYS if os.environ.get(key)
            }
    if args.cmd == "start" and args.debug_streams:
        payload["debug_streams"] = True
    if args.cmd == "set-mode":
        payload["mode"] = args.mode
    if args.cmd == "set-input-target":
        payload["input_target"] = args.input_target
    if args.cmd == "switch-profile":
        payload["profile"] = args.profile
    if args.cmd == "inject-text":
        payload["text"] = args.text
        if args.backends:
            payload["backends"] = args.backends
        if args.no_trailing_space:
            payload["append_trailing_space"] = False
    if args.cmd == "simulate-divergence":
        payload["live"] = args.live
        payload["final"] = args.final
        payload["confirm_delete"] = args.confirm_delete
        if args.backends:
            payload["backends"] = args.backends
        if args.no_trailing_space:
            payload["append_trailing_space"] = False
    if args.cmd == "safe-test":
        payload["text"] = args.text
        payload["backends"] = ["clipboard"]
        payload["append_trailing_space"] = False
    if args.cmd == "external-event":
        payload["type"] = args.type
        payload["session_id"] = args.session_id
        payload["source"] = args.source
        payload["text"] = args.text
        if args.seq is not None:
            payload["seq"] = args.seq
        if args.error:
            payload["error"] = args.error
        if args.no_trailing_space:
            payload["append_trailing_space"] = False
    return payload


def print_response(response: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(response, indent=2, ensure_ascii=True))
        return

    if not response.get("ok"):
        print(f"error: {response.get('error', 'unknown error')}")
        return

    if "message" in response:
        print(response["message"])

    if "backend" in response and response["backend"]:
        print(f"backend={response['backend']}")

    if "action" in response and response["action"]:
        print(f"action={response['action']}")

    status = response.get("status")
    if isinstance(status, dict):
        print(
            f"mode={status.get('mode')} profile={status.get('profile')} "
            f"recording={status.get('recording')} "
            f"activity={status.get('activity_state', 'idle')} "
            f"input_target={status.get('input_target') or '<default>'} "
            f"session_input_target={status.get('session_input_target') or '<none>'} "
            f"model={status.get('model', {}).get('active_device')}/"
            f"{status.get('model', {}).get('active_compute_type')}"
        )

    if "preview" in response:
        print(response["preview"])

    if response.get("text"):
        print(response["text"])


def main() -> int:
    args = parse_args()
    socket_path = load_socket_path(args.config)

    try:
        response = send_command(socket_path, build_payload(args), timeout=args.timeout)
    except FileNotFoundError:
        print(f"error: socket not found: {socket_path}")
        return 1
    except (ConnectionRefusedError, socket.timeout) as exc:
        print(f"error: daemon not reachable: {exc}")
        return 1
    except Exception as exc:
        print(f"error: {exc}")
        return 1

    print_response(response, args.json)
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
