#!/usr/bin/env python3
"""Bind X11 keyboard extra keys to dictation actions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import signal
import subprocess
import sys
import time
from pathlib import Path

from Xlib import X, XK, error
from Xlib.display import Display


ROOT = Path(__file__).resolve().parents[1]
TOGGLE = f"/usr/bin/fish {ROOT / 'scripts/ptt_toggle_commit.fish'}"
LOG = ROOT / "logs/x11_keyboard_dictation.log"

SHIFT = X.ShiftMask
CAPS = X.LockMask
CONTROL = X.ControlMask
ALT = X.Mod1Mask
META = X.Mod4Mask
NUMLOCK_GUESS = X.Mod2Mask
BINDING_MOD_MASK = SHIFT | CONTROL | ALT | META

KEYSYM_FALLBACKS = {
    "XF86HomePage": 0x1008FF18,
    "XF86Mail": 0x1008FF19,
    "XF86PowerDown": 0x1008FF21,
    "XF86PowerOff": 0x1008FF2A,
    "XF86AudioPlay": 0x1008FF14,
    "XF86AudioPause": 0x1008FF31,
    "XF86AudioPlayPause": 0x1008FF14,
}

MODIFIER_NAMES = {
    "shift": SHIFT,
    "ctrl": CONTROL,
    "control": CONTROL,
    "alt": ALT,
    "meta": META,
    "super": META,
}


@dataclass(frozen=True)
class Binding:
    modifier: int
    key_name: str
    keycode: int
    label: str
    command: str
    action_name: str


def modifier_label(state: int) -> str:
    labels = []
    if state & SHIFT:
        labels.append("shift")
    if state & CONTROL:
        labels.append("ctrl")
    if state & ALT:
        labels.append("alt")
    if state & META:
        labels.append("meta")
    if state & CAPS:
        labels.append("caps")
    if state & NUMLOCK_GUESS:
        labels.append("numlock")
    return "+".join(labels) if labels else "none"


def modifier_variants(base: int) -> list[int]:
    variants = []
    for caps in (0, CAPS):
        for num in (0, NUMLOCK_GUESS):
            variants.append(base | caps | num)
    return sorted(set(variants))


def normalized_modifier_state(state: int) -> int:
    return state & BINDING_MOD_MASK


def parse_modifier(value: str) -> int:
    text = value.strip().lower()
    if text in {"", "none"}:
        return 0

    mask = 0
    for part in text.replace("+", ",").split(","):
        name = part.strip()
        if not name:
            continue
        if name not in MODIFIER_NAMES:
            raise ValueError(f"unknown modifier '{name}' in '{value}'")
        mask |= MODIFIER_NAMES[name]
    return mask


def load_xf86_keysyms() -> None:
    loader = getattr(XK, "load_keysym_group", None)
    if loader is None:
        return
    try:
        loader("xf86")
    except Exception:
        pass


def resolve_keysym(name: str) -> int:
    load_xf86_keysyms()
    keysym = XK.string_to_keysym(name)
    if keysym:
        return int(keysym)
    return KEYSYM_FALLBACKS.get(name, 0)


def parse_keys(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def action_slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-") or "key"


def grab_key(root, binding: Binding, owner_events: bool = False) -> None:
    for mod in modifier_variants(binding.modifier):
        try:
            root.grab_key(
                binding.keycode,
                mod,
                owner_events,
                X.GrabModeAsync,
                X.GrabModeAsync,
            )
        except error.BadAccess:
            print(
                f"warning: could not grab key={binding.key_name} keycode={binding.keycode} "
                f"modifier={mod}; another client may own it",
                file=sys.stderr,
            )


def append_log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def run_action(name: str, command: str) -> None:
    append_log(f"action={name} command={command}")
    subprocess.Popen(
        command,
        shell=True,
        stdout=(LOG.parent / f"{name}.out").open("ab"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def key_name_for_event(display: Display, keycode: int) -> str:
    for index in range(4):
        keysym = display.keycode_to_keysym(keycode, index)
        if keysym:
            name = XK.keysym_to_string(keysym)
            if name:
                return name
            return hex(int(keysym))
    return "<none>"


def resolve_bindings(
    display: Display,
    key_names: list[str],
    modifier: str,
    toggle_command: str,
    extra_bindings: list[list[str]],
) -> list[Binding]:
    bindings: list[Binding] = []
    modifier_mask = parse_modifier(modifier)

    for key_name in key_names:
        keysym = resolve_keysym(key_name)
        keycode = int(display.keysym_to_keycode(keysym)) if keysym else 0
        if keycode <= 0:
            print(f"warning: no X11 keycode mapped for keysym {key_name}", file=sys.stderr)
            continue
        bindings.append(
            Binding(
                modifier_mask,
                key_name,
                keycode,
                f"{key_name}=toggle",
                toggle_command,
                f"toggle-{action_slug(key_name)}",
            )
        )

    for raw_modifier, raw_key, label, command in extra_bindings:
        keysym = resolve_keysym(raw_key)
        keycode = int(display.keysym_to_keycode(keysym)) if keysym else 0
        if keycode <= 0:
            print(f"warning: no X11 keycode mapped for keysym {raw_key}", file=sys.stderr)
            continue
        bindings.append(
            Binding(
                parse_modifier(raw_modifier),
                raw_key,
                keycode,
                label,
                command,
                label,
            )
        )

    return bindings


def probe(keys: list[str]) -> int:
    display = Display()
    root = display.screen().root
    bindings = resolve_bindings(display, keys, "none", ":", [])
    for binding in bindings:
        try:
            root.grab_key(
                binding.keycode,
                X.AnyModifier,
                False,
                X.GrabModeAsync,
                X.GrabModeAsync,
            )
        except error.BadAccess:
            print(
                f"warning: could not probe-grab key={binding.key_name} keycode={binding.keycode}",
                file=sys.stderr,
            )
    display.sync()
    print(f"Probing X11 keys {', '.join(keys)}. Press candidate keys; Ctrl+C exits.", flush=True)

    while True:
        event = display.next_event()
        if event.type == X.KeyPress:
            print(
                f"keycode={event.detail} keysym={key_name_for_event(display, event.detail)} "
                f"modifiers={modifier_label(event.state)} raw_state={event.state}",
                flush=True,
            )


def daemon(
    keys: list[str],
    modifier: str,
    toggle_command: str,
    extra_bindings: list[list[str]],
    debounce_seconds: float,
) -> int:
    display = Display()
    root = display.screen().root
    bindings = resolve_bindings(display, keys, modifier, toggle_command, extra_bindings)
    if not bindings:
        print("error: no keyboard bindings resolved", file=sys.stderr)
        return 2

    for binding in bindings:
        grab_key(root, binding)
    display.sync()

    summary_parts = []
    for binding in bindings:
        combo = f"{modifier_label(binding.modifier)}+"
        if combo == "none+":
            combo = ""
        summary_parts.append(f"{combo}{binding.key_name}={binding.label}")
    summary = ", ".join(summary_parts)
    print(f"Keyboard dictation daemon active: {summary}. Ctrl+C exits.", flush=True)
    append_log(f"daemon bindings={summary}")

    last_action_at: dict[str, float] = {}
    while True:
        event = display.next_event()
        if event.type != X.KeyPress:
            continue

        keycode = int(event.detail)
        event_modifier = normalized_modifier_state(int(event.state))
        now = time.monotonic()
        for binding in bindings:
            if keycode != binding.keycode or event_modifier != binding.modifier:
                continue
            previous = last_action_at.get(binding.action_name, 0.0)
            if now - previous < debounce_seconds:
                append_log(f"debounced action={binding.action_name}")
                break
            last_action_at[binding.action_name] = now
            print(binding.label, flush=True)
            run_action(binding.action_name, binding.command)
            break


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--keys", default="XF86HomePage,XF86Mail,XF86PowerOff,XF86PowerDown")
    parser.add_argument("--modifier", default="none", help="Modifier combo, e.g. none or ctrl+shift")
    parser.add_argument("--toggle-command", default=TOGGLE)
    parser.add_argument("--debounce-seconds", type=float, default=0.45)
    parser.add_argument(
        "--binding",
        action="append",
        nargs=4,
        metavar=("MODIFIER", "KEYSYM", "LABEL", "COMMAND"),
        default=[],
        help="Add an exact binding, e.g. --binding none XF86AudioPlay play-toggle 'fish ...'",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, lambda _sig, _frame: raise_keyboard_interrupt())
    try:
        if args.probe:
            return probe(parse_keys(args.keys))
        return daemon(
            parse_keys(args.keys),
            args.modifier,
            args.toggle_command,
            args.binding,
            args.debounce_seconds,
        )
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return 0


def raise_keyboard_interrupt() -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    raise SystemExit(main())
