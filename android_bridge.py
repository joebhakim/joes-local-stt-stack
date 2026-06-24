#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from dictatectl import DEFAULT_CONFIG_PATH, load_socket_path, send_command


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/stt"
PIN_DIGITS = 6


def load_root(config_path: Path) -> Path:
    return config_path.resolve().parent


def default_token_path(config_path: Path) -> Path:
    return load_root(config_path) / "state" / "android_bridge_token.txt"


def ensure_token(path: Path) -> str:
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token

    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{secrets.randbelow(10 ** PIN_DIGITS):0{PIN_DIGITS}d}"
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


def json_response(ok: bool, **fields: Any) -> str:
    payload = {"ok": ok}
    payload.update(fields)
    return json.dumps(payload, ensure_ascii=True)


def connection_path(websocket: Any) -> str:
    request = getattr(websocket, "request", None)
    path = getattr(request, "path", None)
    if isinstance(path, str):
        return path
    legacy_path = getattr(websocket, "path", None)
    return legacy_path if isinstance(legacy_path, str) else ""


class AndroidBridge:
    def __init__(
        self,
        *,
        config_path: Path,
        token_path: Path,
        host: str,
        port: int,
        path: str,
        control_timeout: float,
    ):
        self.config_path = config_path.resolve()
        self.socket_path = load_socket_path(self.config_path)
        self.token_path = token_path.resolve()
        self.token = ensure_token(self.token_path)
        self.host = host
        self.port = port
        self.path = path
        self.control_timeout = control_timeout

    async def serve(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - user environment dependent
            print(
                "Missing dependency: websockets. Install project dependencies before "
                "starting the Android bridge.",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc

        print(f"Android bridge listening on ws://{self.host}:{self.port}{self.path}")
        print(f"Token file: {self.token_path}")
        async with websockets.serve(self.handle, self.host, self.port):
            await asyncio.Future()

    async def handle(self, websocket: Any) -> None:
        if self.path and connection_path(websocket) not in {"", self.path}:
            await websocket.close(code=1008, reason="invalid path")
            return

        session_id = ""
        session_active = False
        authed = False

        try:
            first_raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            first = self.parse_message(first_raw)
            if first.get("type") != "hello" or first.get("token") != self.token:
                await websocket.send(json_response(False, error="authentication failed"))
                await websocket.close(code=1008, reason="authentication failed")
                return
            authed = True
            await websocket.send(json_response(True, type="hello", message="authenticated"))

            async for raw in websocket:
                message = self.parse_message(raw)
                event_type = str(message.get("type", "")).strip().lower()
                if event_type == "hello":
                    await websocket.send(json_response(True, type="hello", message="already authenticated"))
                    continue
                if event_type not in {"start", "partial", "final", "cancel", "error", "end"}:
                    await websocket.send(json_response(False, error=f"unknown event type: {event_type}"))
                    continue

                if event_type == "start":
                    session_id = str(message.get("session_id", "")).strip()
                    session_active = bool(session_id)
                elif not str(message.get("session_id", "")).strip() and session_id:
                    message["session_id"] = session_id

                response = self.forward_event(message)
                await websocket.send(json_response(bool(response.get("ok")), response=response))

                if event_type in {"final", "cancel", "error", "end"}:
                    session_active = False
                    session_id = ""
        except Exception as exc:
            if exc.__class__.__name__ == "ConnectionClosed":
                return
            if authed:
                try:
                    await websocket.send(json_response(False, error=str(exc)))
                except Exception:
                    pass
        finally:
            if session_active and session_id:
                self.forward_event(
                    {
                        "type": "cancel",
                        "session_id": session_id,
                        "source": "android",
                        "error": "android bridge disconnected",
                    }
                )

    @staticmethod
    def parse_message(raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        payload = json.loads(str(raw))
        if not isinstance(payload, dict):
            raise ValueError("message must be a JSON object")
        return payload

    def forward_event(self, message: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "cmd": "external_event",
            "type": message.get("type"),
            "session_id": message.get("session_id", ""),
            "source": message.get("source", "android"),
            "seq": message.get("seq"),
            "text": message.get("text", ""),
            "error": message.get("error", ""),
        }
        if "append_trailing_space" in message:
            payload["append_trailing_space"] = message["append_trailing_space"]
        return send_command(self.socket_path, payload, timeout=self.control_timeout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Android STT WebSocket bridge")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--token-file", type=Path, default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--path", default=DEFAULT_PATH)
    serve.add_argument("--control-timeout", type=float, default=4.0)

    sub.add_parser("token")
    sub.add_parser("pin")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token_path = args.token_file or default_token_path(args.config)

    if args.cmd in {"token", "pin"}:
        print(ensure_token(token_path))
        return 0

    bridge = AndroidBridge(
        config_path=args.config,
        token_path=token_path,
        host=args.host,
        port=args.port,
        path=args.path,
        control_timeout=args.control_timeout,
    )
    try:
        asyncio.run(bridge.serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
