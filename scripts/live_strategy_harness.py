#!/usr/bin/env python3
"""Offline checks for live transcription append strategies.

The default synthetic suite is quick and deterministic. Audio mode runs
faster-whisper over rolling windows without injecting text.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dictate import (
    compare_token,
    detokenize,
    normalize_text,
    read_audio_with_ffmpeg,
    rolling_append_final_remainder,
    suffix_prefix_overlap_len_fuzzy,
    tokenize,
    trim_prefix_already_in_suffix,
)


DEFAULT_AUDIO_FILES = [
    Path("jfk.wav"),
]


@dataclass
class StepResult:
    index: int
    source: str
    committed_overlap: int
    prev_overlap: int
    current: str
    appended: str
    committed: str


def count_subsequence(tokens: list[str], phrase: str) -> int:
    needle = [compare_token(token) for token in tokenize(phrase)]
    haystack = [compare_token(token) for token in tokens]
    if not needle:
        return 0
    count = 0
    width = len(needle)
    for idx in range(0, len(haystack) - width + 1):
        if haystack[idx : idx + width] == needle:
            count += 1
    return count


def repeated_ngram_count(tokens: list[str], n: int = 4) -> int:
    if len(tokens) < n:
        return 0
    seen: set[tuple[str, ...]] = set()
    repeats = 0
    normalized = [compare_token(token) for token in tokens]
    for idx in range(0, len(normalized) - n + 1):
        gram = tuple(normalized[idx : idx + n])
        if gram in seen:
            repeats += 1
        else:
            seen.add(gram)
    return repeats


def simulate_rolling_append(
    transcripts: list[str],
    *,
    stable_rounds: int = 1,
    min_overlap_tokens: int = 3,
    committed_tail_tokens: int = 32,
) -> tuple[str, list[StepResult]]:
    committed: list[str] = []
    prev: list[str] = []
    pending: list[str] = []
    rounds = 0
    steps: list[StepResult] = []

    for index, transcript in enumerate(transcripts, start=1):
        current = tokenize(transcript)
        committed_tail = committed[-committed_tail_tokens:]
        committed_overlap = suffix_prefix_overlap_len_fuzzy(committed_tail, current)
        prev_overlap = suffix_prefix_overlap_len_fuzzy(prev, current)

        if not committed and not prev:
            candidate = current
            source = "initial"
        elif committed_overlap >= min_overlap_tokens:
            candidate = current[committed_overlap:]
            source = "committed_tail"
        elif not committed and prev_overlap >= min_overlap_tokens:
            candidate = current[prev_overlap:]
            source = "prev_window"
        else:
            candidate = []
            source = "unaligned"

        if candidate:
            candidate = candidate[:-1] if len(candidate) > 1 else []
            candidate = trim_prefix_already_in_suffix(committed, candidate)

        if candidate == pending:
            rounds += 1
        else:
            pending = list(candidate)
            rounds = 1

        appended: list[str] = []
        if rounds >= stable_rounds and candidate:
            appended = list(candidate)
            committed.extend(appended)
            pending = []
            rounds = 0

        steps.append(
            StepResult(
                index=index,
                source=source,
                committed_overlap=committed_overlap,
                prev_overlap=prev_overlap,
                current=detokenize(current),
                appended=detokenize(appended),
                committed=detokenize(committed),
            )
        )
        prev = current

    return detokenize(committed), steps


def simulate_draft_replace(transcripts: list[str]) -> tuple[str, list[str]]:
    draft = ""
    drafts: list[str] = []
    for transcript in transcripts:
        tokens = tokenize(transcript)
        draft_tokens = tokens[:-1] if len(tokens) > 1 else tokens
        draft = detokenize(draft_tokens)
        drafts.append(draft)
    return draft, drafts


def run_synthetic_suite(verbose: bool) -> int:
    cases = [
        {
            "name": "expanding_prefix",
            "transcripts": [
                "this is a test",
                "this is a test of the rolling",
                "this is a test of the rolling append strategy",
                "test of the rolling append strategy right now",
                "rolling append strategy right now and it should not repeat",
            ],
            "must_include": ["this is a test", "rolling append strategy"],
            "max_phrase_counts": {"this is a test": 1, "rolling append strategy": 1},
            "max_repeated_4grams": 0,
        },
        {
            "name": "append_vs_a_pen_rewrite",
            "transcripts": [
                "I'm talking about the rolling a pen",
                "I'm talking about the rolling append strategy right now",
                "the rolling append strategy right now and the quality is terrible",
                "rolling append strategy right now and the quality is terrible and redundant",
            ],
            "must_include": ["I'm talking about", "rolling"],
            "max_phrase_counts": {"I'm talking about": 1},
            "max_repeated_4grams": 0,
        },
        {
            "name": "unaligned_rewrite_waits",
            "transcripts": [
                "alpha beta gamma delta epsilon",
                "unrelated rewrite with no useful overlap",
                "another unrelated rewrite with no useful overlap",
            ],
            "must_include": ["alpha beta"],
            "max_phrase_counts": {"unrelated rewrite": 0},
            "max_repeated_4grams": 0,
        },
    ]

    failures: list[str] = []
    for case in cases:
        committed, steps = simulate_rolling_append(case["transcripts"])
        tokens = tokenize(committed)
        print(f"case={case['name']} tokens={len(tokens)} repeated4={repeated_ngram_count(tokens)}")
        print(f"  committed: {committed}")
        if verbose:
            for step in steps:
                print(
                    f"  step={step.index} src={step.source} "
                    f"co={step.committed_overlap} po={step.prev_overlap} append={step.appended!r}"
                )

        for phrase in case["must_include"]:
            if count_subsequence(tokens, phrase) < 1:
                failures.append(f"{case['name']}: missing phrase {phrase!r}")
        for phrase, max_count in case["max_phrase_counts"].items():
            count = count_subsequence(tokens, phrase)
            if count > max_count:
                failures.append(
                    f"{case['name']}: phrase {phrase!r} count {count} > {max_count}"
                )
        repeated = repeated_ngram_count(tokens)
        if repeated > case["max_repeated_4grams"]:
            failures.append(
                f"{case['name']}: repeated 4-grams {repeated} > {case['max_repeated_4grams']}"
            )

    draft_text, drafts = simulate_draft_replace(
        [
            "This is a",
            "This is the perfect",
            "This is the perfect mode right now",
        ]
    )
    print(f"draft_case=replace_rewrite final_draft={draft_text!r}")
    if verbose:
        for idx, draft in enumerate(drafts, start=1):
            print(f"  draft_step={idx} draft={draft!r}")
    if draft_text != "This is the perfect mode right":
        failures.append(
            f"replace_rewrite: final draft {draft_text!r} did not track latest transcript"
        )
    if count_subsequence(tokenize(' '.join(drafts)), "This is a This is the") > 0:
        failures.append("replace_rewrite: drafts were concatenated instead of replaced")

    final_cases = [
        {
            "name": "final_remainder_after_clean_prefix",
            "live": "this is a test",
            "final": "this is a test of final commit",
            "removal_ok": True,
            "want_action": "append_remainder",
            "want_remainder": "of final commit",
        },
        {
            "name": "final_replace_after_live_rewrite",
            "live": "This is a",
            "final": "This is the perfect mode, let's see what it says.",
            "removal_ok": True,
            "want_action": "replace_live_with_final",
            "want_remainder": "This is the perfect mode, let's see what it says.",
        },
        {
            "name": "final_skip_if_live_removal_fails",
            "live": "This is a",
            "final": "This is the perfect mode, let's see what it says.",
            "removal_ok": False,
            "want_action": "skip_removal_failed",
            "want_remainder": "",
        },
    ]
    for case in final_cases:
        remainder_tokens, action, prefix_len = rolling_append_final_remainder(
            tokenize(case["live"]),
            tokenize(case["final"]),
            live_text_removal_ok=case["removal_ok"],
        )
        remainder = detokenize(remainder_tokens)
        print(
            f"final_case={case['name']} action={action} prefix={prefix_len} "
            f"remainder={remainder!r}"
        )
        if action != case["want_action"]:
            failures.append(
                f"{case['name']}: action {action!r} != {case['want_action']!r}"
            )
        if remainder != case["want_remainder"]:
            failures.append(
                f"{case['name']}: remainder {remainder!r} != {case['want_remainder']!r}"
            )

    if failures:
        print("FAIL")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("synthetic suite passed")
    return 0


def transcribe_windows(
    audio_path: Path,
    *,
    model_name: str,
    device: str,
    compute_type: str,
    window_seconds: float,
    step_seconds: float,
    max_duration_seconds: float,
    repeat: int,
) -> tuple[list[str], str, float]:
    from faster_whisper import WhisperModel

    pcm = read_audio_with_ffmpeg(audio_path, 16000, 1)
    samples = np.frombuffer(pcm, dtype=np.int16)
    if repeat > 1:
        samples = np.tile(samples, repeat)
    if max_duration_seconds > 0:
        samples = samples[: int(max_duration_seconds * 16000)]

    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    duration_seconds = len(samples) / 16000.0
    transcripts: list[str] = []
    start = time.perf_counter()
    end = max(1.5, min(window_seconds, duration_seconds))
    while end <= duration_seconds + 1e-6:
        window_start = max(0.0, end - window_seconds)
        chunk = samples[int(window_start * 16000) : int(end * 16000)]
        audio = chunk.astype(np.float32) / 32768.0
        segments, _ = model.transcribe(
            audio,
            beam_size=1,
            language="en",
            vad_filter=False,
            condition_on_previous_text=False,
        )
        transcripts.append(normalize_text(" ".join(seg.text.strip() for seg in segments)))
        end += step_seconds

    full_audio = samples.astype(np.float32) / 32768.0
    segments, _ = model.transcribe(
        full_audio,
        beam_size=1,
        language="en",
        vad_filter=False,
        condition_on_previous_text=False,
    )
    final_text = normalize_text(" ".join(seg.text.strip() for seg in segments))
    return transcripts, final_text, time.perf_counter() - start


def run_audio_suite(args: argparse.Namespace) -> int:
    audio_files = [Path(path) for path in args.audio] if args.audio else DEFAULT_AUDIO_FILES
    failures = 0
    for audio_path in audio_files:
        if not audio_path.exists():
            print(f"audio missing: {audio_path}")
            failures += 1
            continue

        print(f"audio={audio_path}")
        transcripts, final_text, elapsed = transcribe_windows(
            audio_path,
            model_name=args.model,
            device=args.device,
            compute_type=args.compute_type,
            window_seconds=args.window_seconds,
            step_seconds=args.step_seconds,
            max_duration_seconds=args.max_duration_seconds,
            repeat=args.repeat,
        )
        if args.strategy == "rolling_append":
            committed, steps = simulate_rolling_append(transcripts)
            tokens = tokenize(committed)
            unaligned = sum(1 for step in steps if step.source == "unaligned")
            print(
                f"  strategy=rolling_append windows={len(steps)} tokens={len(tokens)} "
                f"repeated4={repeated_ngram_count(tokens)} unaligned={unaligned} "
                f"elapsed_s={elapsed:.2f}"
            )
            print(f"  live_committed: {committed[:240]}")
            print(f"  final_commit:   {final_text[:240]}")
            if args.verbose:
                for step in steps:
                    print(
                        f"  step={step.index:02d} src={step.source:<14} "
                        f"co={step.committed_overlap:2d} po={step.prev_overlap:2d} "
                        f"append={step.appended!r}"
                    )
        else:
            final_draft, drafts = simulate_draft_replace(transcripts)
            final_tokens = tokenize(final_text)
            draft_tokens = tokenize(final_draft)
            print(
                f"  strategy=draft_replace windows={len(drafts)} draft_tokens={len(draft_tokens)} "
                f"final_tokens={len(final_tokens)} final_repeated4={repeated_ngram_count(final_tokens)} "
                f"elapsed_s={elapsed:.2f}"
            )
            print(f"  live_draft:   {final_draft[:240]}")
            print(f"  final_commit: {final_text[:240]}")
            if args.verbose:
                for idx, (transcript, draft) in enumerate(zip(transcripts, drafts), start=1):
                    print(
                        f"  step={idx:02d} transcript={transcript!r} draft={draft!r}"
                    )
    return 1 if failures else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-only", action="store_true")
    parser.add_argument("--audio", action="append", default=[])
    parser.add_argument("--model", default="tiny.en")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--strategy", choices=["draft_replace", "rolling_append"], default="draft_replace")
    parser.add_argument("--window-seconds", type=float, default=5.0)
    parser.add_argument("--step-seconds", type=float, default=1.0)
    parser.add_argument("--max-duration-seconds", type=float, default=24.0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    synthetic_status = run_synthetic_suite(verbose=args.verbose)
    if synthetic_status != 0 or args.synthetic_only:
        return synthetic_status
    return run_audio_suite(args)


if __name__ == "__main__":
    raise SystemExit(main())
