#!/usr/bin/env python3
"""Bind X11 mouse buttons to dictation actions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import signal
import subprocess
import sys
import time
from pathlib import Path

from Xlib import X, error
from Xlib.display import Display


ROOT = Path(__file__).resolve().parents[1]
START = f"/usr/bin/fish {ROOT / 'scripts/ptt_press.fish'}"
COMMIT = f"/usr/bin/fish {ROOT / 'scripts/ptt_release_commit.fish'}"
LOG = ROOT / "logs/x11_mouse_dictation.log"

SHIFT = X.ShiftMask
CAPS = X.LockMask
CONTROL = X.ControlMask
ALT = X.Mod1Mask
META = X.Mod4Mask
NUMLOCK_GUESS = X.Mod2Mask
BINDING_MOD_MASK = SHIFT | CONTROL | ALT | META

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
    button: int
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


def grab_button(root, button: int, modifier: int, owner_events: bool = True) -> None:
    for mod in modifier_variants(modifier):
        try:
            root.grab_button(
                button,
                mod,
                owner_events,
                X.ButtonPressMask,
                X.GrabModeAsync,
                X.GrabModeAsync,
                X.NONE,
                X.NONE,
            )
        except error.BadAccess:
            print(f"warning: could not grab button={button} modifier={mod}", file=sys.stderr)


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


def probe(buttons: list[int]) -> int:
    display = Display()
    root = display.screen().root
    for button in buttons:
        root.grab_button(
            button,
            X.AnyModifier,
            True,
            X.ButtonPressMask,
            X.GrabModeAsync,
            X.GrabModeAsync,
            X.NONE,
            X.NONE,
        )
    display.sync()
    print(f"Probing X11 buttons {buttons}. Press candidate mouse buttons; Ctrl+C exits.", flush=True)

    while True:
        event = display.next_event()
        if event.type == X.ButtonPress:
            print(
                f"button={event.detail} modifiers={modifier_label(event.state)} raw_state={event.state}",
                flush=True,
            )


def daemon(
    start_button: int,
    commit_button: int,
    debug_button: int,
    modifier: str,
    record_command: str,
    commit_command: str,
    debug_command: str,
    record_label: str,
    commit_label: str,
    debug_label: str,
    extra_bindings: list[list[str]],
) -> int:
    display = Display()
    root = display.screen().root
    modifier_mask = parse_modifier(modifier)
    bindings = [
        Binding(modifier_mask, start_button, record_label, record_command, "record"),
        Binding(modifier_mask, commit_button, commit_label, commit_command, "commit"),
    ]
    if debug_button > 0:
        bindings.append(Binding(modifier_mask, debug_button, debug_label, debug_command, "debug-record"))
    for raw_modifier, raw_button, label, command in extra_bindings:
        bindings.append(
            Binding(
                parse_modifier(raw_modifier),
                int(raw_button),
                label,
                command,
                label,
            )
        )

    for binding in bindings:
        grab_button(root, binding.button, binding.modifier)
    display.sync()

    summary_parts = []
    for binding in bindings:
        combo = f"{modifier_label(binding.modifier)}+"
        if combo == "none+":
            combo = ""
        summary_parts.append(f"{combo}Button{binding.button}={binding.label}")
    summary = ", ".join(summary_parts)
    print(f"Mouse dictation daemon active: {summary}. Ctrl+C exits.", flush=True)
    append_log(f"daemon bindings={summary}")

    while True:
        event = display.next_event()
        if event.type != X.ButtonPress:
            continue

        button = int(event.detail)
        event_modifier = normalized_modifier_state(int(event.state))
        for binding in bindings:
            if button == binding.button and event_modifier == binding.modifier:
                print(binding.label, flush=True)
                run_action(binding.action_name, binding.command)
                break


def parse_buttons(value: str) -> list[int]:
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--buttons", default="4,5,8,9,10")
    parser.add_argument("--start-button", type=int, default=9)
    parser.add_argument("--commit-button", type=int, default=8)
    parser.add_argument("--debug-button", type=int, default=0)
    parser.add_argument("--modifier", default="shift", help="Modifier combo, e.g. shift or ctrl+shift")
    parser.add_argument("--record-command", default=START)
    parser.add_argument("--commit-command", default=COMMIT)
    parser.add_argument("--debug-command", default=f"{START} --debug-streams")
    parser.add_argument("--record-label", default="record")
    parser.add_argument("--commit-label", default="commit")
    parser.add_argument("--debug-label", default="debug-record")
    parser.add_argument(
        "--binding",
        action="append",
        nargs=4,
        metavar=("MODIFIER", "BUTTON", "LABEL", "COMMAND"),
        default=[],
        help="Add an exact binding, e.g. --binding ctrl+shift 8 fast-record 'fish ...'",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, lambda _sig, _frame: raise_keyboard_interrupt())
    try:
        if args.probe:
            return probe(parse_buttons(args.buttons))
        return daemon(
            args.start_button,
            args.commit_button,
            args.debug_button,
            args.modifier,
            args.record_command,
            args.commit_command,
            args.debug_command,
            args.record_label,
            args.commit_label,
            args.debug_label,
            args.binding,
        )
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return 0


def raise_keyboard_interrupt() -> None:
    raise KeyboardInterrupt


if __name__ == "__main__":
    raise SystemExit(main())
