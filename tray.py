#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dictatectl import DEFAULT_CONFIG_PATH, load_socket_path, send_command

try:
    from PyQt6.QtCore import QPoint, QRectF, Qt, QTimer
    from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
    from PyQt6.QtWidgets import (
        QApplication,
        QComboBox,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QMenu,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
    )
except ImportError:
    try:
        from PySide6.QtCore import QPoint, QRectF, Qt, QTimer
        from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
        from PySide6.QtWidgets import (
            QApplication,
            QComboBox,
            QFrame,
            QGridLayout,
            QHBoxLayout,
            QLabel,
            QMenu,
            QProgressBar,
            QPushButton,
            QScrollArea,
            QSystemTrayIcon,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        print(
            "Missing Qt bindings. Install one of: python-pyqt6 or python-pyside6",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


@dataclass(frozen=True)
class VisualState:
    key: str
    label: str
    color: str


STATE_OFFLINE = VisualState("offline", "Offline", "#6b7280")
STATE_IDLE = VisualState("idle", "Idle (PTT)", "#16a34a")
STATE_LIVE = VisualState("live", "Live Mode", "#0ea5e9")
STATE_RECORDING_REJECTING = VisualState(
    "recording_rejecting",
    "Recording: Rejecting Silence",
    "#f97316",
)
STATE_RECORDING_ACCEPTING = VisualState(
    "recording_accepting",
    "Recording: Accepting Voice",
    "#dc2626",
)
STATE_FINALIZING = VisualState("finalizing", "Finalizing", "#8b5cf6")
STATE_COMMITTED = VisualState("committed", "Committed", "#22c55e")
STATE_NO_VOICE = VisualState("no_voice", "No Voice", "#f97316")
STATE_CANCELLED = VisualState("cancelled", "Cancelled", "#64748b")
STATE_ERROR = VisualState("error", "Error", "#f59e0b")


def event_global_pos(event: Any) -> QPoint:
    if hasattr(event, "globalPosition"):
        return event.globalPosition().toPoint()
    return event.globalPos()


class FloatingHud(QWidget):
    WIDTH = 430
    DETAIL_WIDTH = 720

    def __init__(self, owner: "DictationTray"):
        super().__init__(None)
        self.owner = owner
        self.position_path = owner.config_path.parent / "state" / "hud_position.json"
        self.preview_path = owner.config_path.parent / "state" / "preview.txt"
        self.details_expanded = False
        self._syncing_profile_combo = False
        self._drag_offset: QPoint | None = None

        self.setWindowTitle("STT HUD")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumWidth(self.WIDTH)

        self._build_ui()
        self.adjustSize()
        self.resize(self.WIDTH, self.sizeHint().height())
        self._restore_position()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)

        self.root = QFrame()
        self.root.setObjectName("hudRoot")
        outer.addWidget(self.root)

        layout = QVBoxLayout(self.root)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(9)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(10, 10)
        self.title_label = QLabel("STT")
        self.title_label.setObjectName("hudTitle")
        self.state_label = QLabel("Offline")
        self.state_label.setObjectName("hudState")
        self.details_button = QPushButton("Details")
        self.tray_button = QPushButton("Tray")
        for button in (self.details_button, self.tray_button):
            button.setFixedHeight(24)
        header.addWidget(self.status_dot)
        header.addWidget(self.title_label)
        header.addWidget(self.state_label, 1)
        header.addWidget(self.details_button)
        header.addWidget(self.tray_button)
        layout.addLayout(header)

        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.profile_label = QLabel("profile=unknown")
        self.profile_combo = QComboBox()
        self.source_label = QLabel("source=unknown")
        self.gate_label = QLabel("gate=offline")
        self.profile_label.setObjectName("hudMeta")
        self.profile_combo.setObjectName("hudProfileCombo")
        self.source_label.setObjectName("hudMeta")
        self.gate_label.setObjectName("hudMeta")
        meta.addWidget(self.profile_label)
        for profile_name in self.owner._profile_names():
            self.profile_combo.addItem(profile_name)
        self.profile_combo.setFixedHeight(24)
        self.profile_combo.setToolTip("Switch STT profile")
        meta.addWidget(self.profile_combo)
        meta.addWidget(self.source_label)
        meta.addWidget(self.gate_label, 1)
        layout.addLayout(meta)

        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedHeight(6)
        layout.addWidget(self.level_bar)

        self.committed_caption = QLabel("Accepted")
        self.committed_caption.setObjectName("hudCaption")
        self.committed_text = QLabel("")
        self.committed_text.setObjectName("hudAcceptedText")
        self.committed_text.setWordWrap(True)
        layout.addWidget(self.committed_caption)
        layout.addWidget(self._scroll_wrap(self.committed_text, 72))

        self.partial_caption = QLabel("Mutable Partial")
        self.partial_caption.setObjectName("hudCaption")
        self.partial_text = QLabel("")
        self.partial_text.setObjectName("hudPartialText")
        self.partial_text.setWordWrap(True)
        layout.addWidget(self.partial_caption)
        layout.addWidget(self._scroll_wrap(self.partial_text, 72))

        self.details_scroll = QScrollArea()
        self.details_scroll.setObjectName("hudDetailsScroll")
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.details_scroll.setFixedHeight(360)
        self.details_body = QWidget()
        details_layout = QVBoxLayout(self.details_body)
        details_layout.setContentsMargins(8, 8, 8, 8)
        details_layout.setSpacing(10)

        self.detail_session_fields = self._add_field_section(
            details_layout,
            "Session",
            (
                ("source", "Source"),
                ("state", "State"),
                ("note", "Note"),
                ("mode", "Mode"),
                ("profile", "Profile"),
                ("recording", "Recording"),
                ("activity", "Activity"),
                ("session_id", "Session ID"),
                ("seq", "Seq"),
                ("updated_at", "Updated"),
                ("status_error", "Status Error"),
            ),
        )
        self.detail_audio_fields = self._add_field_section(
            details_layout,
            "Audio / Commit",
            (
                ("audio_gate", "Audio Gate"),
                ("level", "Level"),
                ("live_commit", "Live Commit"),
                ("alignment_lost", "Alignment Lost"),
                ("debug_active", "Debug Active"),
                ("params", "Params"),
                ("debug_params", "Debug Params"),
            ),
        )
        self.detail_last_result_fields = self._add_field_section(
            details_layout,
            "Last Result",
            (
                ("kind", "Kind"),
                ("source", "Source"),
                ("reason", "Reason"),
                ("activity", "Activity"),
                ("elapsed_s", "Elapsed"),
                ("injection_ok", "Injection OK"),
                ("backend", "Backend"),
                ("message", "Message"),
                ("updated_at", "Updated"),
                ("details", "Details"),
            ),
        )
        self.detail_last_result_text = self._add_text_section(details_layout, "Last Final Text")
        self.detail_last_injected_text = self._add_text_section(details_layout, "Last Injected Text")
        self.detail_committed_text = self._add_text_section(details_layout, "Accepted")
        self.detail_tail_text = self._add_text_section(details_layout, "Mutable Tail")
        self.detail_branch_text = self._add_text_section(details_layout, "Branch")
        self.detail_streams_text = self._add_text_section(details_layout, "Simulated Accept Streams")
        self.detail_windows_text = self._add_text_section(details_layout, "Rolling Transcript Windows")
        details_layout.addStretch(1)

        self.details_scroll.setWidget(self.details_body)
        layout.addWidget(self.details_scroll)
        self.details_scroll.hide()

        controls = QHBoxLayout()
        controls.setSpacing(7)
        self.record_button = QPushButton("Record")
        self.commit_button = QPushButton("Commit")
        self.cancel_button = QPushButton("Cancel")
        self.mode_button = QPushButton("Live")
        for button in (
            self.record_button,
            self.commit_button,
            self.cancel_button,
            self.mode_button,
        ):
            button.setFixedHeight(28)
            controls.addWidget(button)
        layout.addLayout(controls)

        self.record_button.clicked.connect(lambda: self.owner._send_control("start"))
        self.commit_button.clicked.connect(lambda: self.owner._send_control("stop_commit"))
        self.cancel_button.clicked.connect(lambda: self.owner._send_control("stop_cancel"))
        self.mode_button.clicked.connect(lambda: self.owner._send_control("toggle_live"))
        self.details_button.clicked.connect(self._toggle_details)
        self.tray_button.clicked.connect(self._collapse_to_tray)
        self.profile_combo.currentTextChanged.connect(self._profile_selected)

        self.setStyleSheet(
            """
            #hudRoot {
                background-color: rgba(18, 20, 22, 234);
                border: 1px solid rgba(255, 255, 255, 40);
                border-radius: 8px;
            }
            QLabel {
                color: #e5e7eb;
                font-size: 12px;
            }
            #hudTitle {
                color: #f8fafc;
                font-size: 15px;
                font-weight: 700;
            }
            #hudState, #hudMeta, #hudCaption {
                color: #9ca3af;
            }
            #hudCaption {
                font-size: 11px;
                font-weight: 700;
                text-transform: uppercase;
            }
            #hudAcceptedText {
                color: #f8fafc;
            }
            #hudPartialText {
                color: #fbbf24;
                font-style: italic;
            }
            #hudDetailsScroll {
                background-color: rgba(5, 8, 12, 190);
                border: 1px solid rgba(255, 255, 255, 24);
                border-radius: 6px;
            }
            #hudDetailFrame {
                background-color: rgba(255, 255, 255, 10);
                border: 1px solid rgba(255, 255, 255, 18);
                border-radius: 6px;
            }
            #hudDetailKey {
                color: #94a3b8;
                font-weight: 700;
            }
            #hudDetailValue {
                color: #e5e7eb;
                font-family: monospace;
                font-size: 11px;
            }
            #hudDetailsBlock {
                background-color: rgba(255, 255, 255, 10);
                border: 1px solid rgba(255, 255, 255, 18);
                border-radius: 6px;
                color: #d1d5db;
                font-family: monospace;
                font-size: 11px;
                padding: 7px;
            }
            QPushButton {
                background-color: #2f6f73;
                color: #f8fafc;
                border: 0;
                border-radius: 6px;
                padding: 4px 9px;
                font-weight: 700;
            }
            QPushButton:disabled {
                background-color: #3f454b;
                color: #8b949e;
            }
            QPushButton:hover:!disabled {
                background-color: #38898d;
            }
            QComboBox#hudProfileCombo {
                background-color: rgba(255, 255, 255, 18);
                color: #e5e7eb;
                border: 1px solid rgba(255, 255, 255, 34);
                border-radius: 5px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: 700;
            }
            QComboBox#hudProfileCombo:disabled {
                color: #7b8490;
                background-color: rgba(255, 255, 255, 8);
            }
            QProgressBar {
                background-color: #35383d;
                border: 0;
                border-radius: 3px;
            }
            QProgressBar::chunk {
                background-color: #fbbf24;
                border-radius: 3px;
            }
            QScrollArea {
                background-color: rgba(255, 255, 255, 12);
                border: 1px solid rgba(255, 255, 255, 22);
                border-radius: 6px;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 70);
                border-radius: 4px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            """
        )

    def _add_field_section(
        self,
        layout: QVBoxLayout,
        title: str,
        rows: tuple[tuple[str, str], ...],
    ) -> dict[str, QLabel]:
        caption = QLabel(title)
        caption.setObjectName("hudCaption")
        layout.addWidget(caption)

        frame = QFrame()
        frame.setObjectName("hudDetailFrame")
        grid = QGridLayout(frame)
        grid.setContentsMargins(8, 7, 8, 7)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(5)

        labels: dict[str, QLabel] = {}
        for row, (key, label_text) in enumerate(rows):
            key_label = QLabel(label_text)
            key_label.setObjectName("hudDetailKey")
            value_label = QLabel("-")
            value_label.setObjectName("hudDetailValue")
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            value_label.setWordWrap(True)
            grid.addWidget(key_label, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(value_label, row, 1)
            labels[key] = value_label

        grid.setColumnStretch(1, 1)
        layout.addWidget(frame)
        return labels

    def _add_text_section(self, layout: QVBoxLayout, title: str) -> QLabel:
        caption = QLabel(title)
        caption.setObjectName("hudCaption")
        layout.addWidget(caption)

        label = QLabel("-")
        label.setObjectName("hudDetailsBlock")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setWordWrap(True)
        label.setMinimumHeight(34)
        layout.addWidget(label)
        return label

    def _toggle_details(self) -> None:
        self.details_expanded = not self.details_expanded
        self.details_button.setText("Compact" if self.details_expanded else "Details")
        self.details_scroll.setVisible(self.details_expanded)
        if self.details_expanded:
            self._refresh_details()

        target_width = self.DETAIL_WIDTH if self.details_expanded else self.WIDTH
        self.setMinimumWidth(target_width)
        self.resize(target_width, self.sizeHint().height())
        self.adjustSize()
        self._save_position()

    def _collapse_to_tray(self) -> None:
        self._save_position()
        self.hide()
        action = getattr(self.owner, "show_hud_action", None)
        if action is not None and hasattr(action, "setChecked"):
            action.setChecked(False)

    def _profile_selected(self, profile_name: str) -> None:
        if self._syncing_profile_combo:
            return
        profile_name = profile_name.strip()
        if not profile_name:
            return
        status = getattr(self, "_last_status", None)
        if isinstance(status, dict) and profile_name == str(status.get("profile") or ""):
            return
        self.owner._send_control("switch_profile", {"profile": profile_name})

    def _read_preview_text(self) -> str:
        try:
            text = self.preview_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return "state/preview.txt has not been written yet."
        except OSError as exc:
            return f"Could not read state/preview.txt: {exc}"

        text = text.rstrip()
        if not text:
            return "state/preview.txt is empty."
        if len(text) > 24000:
            text = "... trimmed to latest diagnostics ...\n" + text[-24000:]
        return text

    def _strip_numbered_line(self, text: str) -> str:
        stripped = text.strip()
        if len(stripped) > 3 and stripped[:2].isdigit() and stripped[2] == " ":
            return stripped[3:]
        return stripped

    def _split_preview_field(self, text: str) -> tuple[str, str] | None:
        stripped = text.strip()
        if not stripped:
            return None
        parts = stripped.split(None, 1)
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], parts[1]

    def _preview_data(self) -> tuple[dict[str, str], dict[str, list[str]]]:
        fields: dict[str, str] = {}
        sections: dict[str, list[str]] = {
            "committed": [],
            "tail": [],
            "debug": [],
            "branch": [],
            "streams": [],
            "windows": [],
        }

        text = self._read_preview_text()
        if not text or text.startswith("state/preview.txt "):
            return fields, sections

        current = "fields"
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped == "personal-stt preview":
                continue

            if stripped in {"committed", "tail", "debug_streams"}:
                current = stripped
                continue
            if stripped == "simulated_accept_streams:":
                current = "streams"
                continue
            if stripped == "rolling_transcript_window:":
                current = "windows"
                continue

            if current in {"committed", "tail"}:
                sections[current].append(self._strip_numbered_line(stripped))
                continue
            if stripped.startswith("branch."):
                sections["branch"].append(stripped)
                parsed = self._split_preview_field(stripped)
                if parsed is not None:
                    key, value = parsed
                    fields[key] = value or "<empty>"
                continue
            if stripped.startswith("rounds"):
                sections["streams"].append(stripped)
                continue
            if stripped.startswith("win"):
                sections["windows"].append(stripped)
                continue

            parsed = self._split_preview_field(stripped)
            if parsed is None:
                continue
            key, value = parsed
            fields[key] = value or "<empty>"
            if current == "debug_streams":
                sections["debug"].append(f"{key} = {fields[key]}")

        return fields, sections

    def _first_present(self, *values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return ""

    def _display_value(self, value: Any) -> str:
        if value is None or value == "":
            return "-"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, dict):
            if not value:
                return "-"
            return " ".join(f"{key}={value[key]}" for key in sorted(value))
        return str(value)

    def _set_field_values(self, labels: dict[str, QLabel], values: dict[str, Any]) -> None:
        for key, label in labels.items():
            label.setText(self._display_value(values.get(key)))

    def _lines_value(self, lines: list[str]) -> str:
        cleaned = [line for line in lines if line.strip()]
        return "\n".join(cleaned) if cleaned else "-"

    def _refresh_details(self) -> None:
        if not self.details_expanded:
            return

        visual = getattr(self, "_last_visual", STATE_OFFLINE)
        status = getattr(self, "_last_status", None)
        status_error = getattr(self, "_last_status_error", "")
        status_dict = status if isinstance(status, dict) else {}
        display = status_dict.get("dictation_display", {})
        if not isinstance(display, dict):
            display = {}
        last_result = status_dict.get("last_result", {})
        if not isinstance(last_result, dict):
            last_result = {}

        preview_fields, preview_sections = self._preview_data()

        self._set_field_values(
            self.detail_session_fields,
            {
                "source": self._first_present(display.get("source"), "desktop"),
                "state": self._first_present(display.get("state"), visual.label),
                "note": self._first_present(display.get("note"), status_dict.get("note")),
                "mode": self._first_present(status_dict.get("mode"), preview_fields.get("mode")),
                "profile": self._first_present(status_dict.get("profile"), preview_fields.get("profile")),
                "recording": self._first_present(status_dict.get("recording"), preview_fields.get("recording")),
                "activity": self._first_present(status_dict.get("activity"), preview_fields.get("activity")),
                "session_id": display.get("session_id"),
                "seq": display.get("seq"),
                "updated_at": display.get("updated_at"),
                "status_error": status_error,
            },
        )
        self._set_field_values(
            self.detail_audio_fields,
            {
                "audio_gate": self._first_present(
                    display.get("audio_gate_label"),
                    status_dict.get("audio_gate_label"),
                    preview_fields.get("audio_gate"),
                ),
                "level": self._first_present(preview_fields.get("level"), display.get("audio_level")),
                "live_commit": self._first_present(status_dict.get("live_commit"), preview_fields.get("live_commit")),
                "alignment_lost": self._first_present(
                    display.get("alignment_lost"),
                    status_dict.get("alignment_lost"),
                    preview_fields.get("alignment_lost"),
                ),
                "debug_active": preview_fields.get("debug_active"),
                "params": preview_fields.get("params"),
                "debug_params": preview_fields.get("debug_params"),
            },
        )
        self._set_field_values(
            self.detail_last_result_fields,
            {
                "kind": last_result.get("kind"),
                "source": last_result.get("source"),
                "reason": last_result.get("reason"),
                "activity": last_result.get("activity"),
                "elapsed_s": last_result.get("elapsed_s"),
                "injection_ok": last_result.get("injection_ok"),
                "backend": last_result.get("backend"),
                "message": last_result.get("message"),
                "updated_at": last_result.get("updated_at"),
                "details": last_result.get("details"),
            },
        )

        committed = self._first_present(
            display.get("committed_text"),
            self._lines_value(preview_sections["committed"]),
        )
        tail = self._first_present(
            display.get("partial_text"),
            self._lines_value(preview_sections["tail"]),
        )

        self.detail_committed_text.setText(self._display_value(committed))
        self.detail_last_result_text.setText(self._display_value(last_result.get("text")))
        self.detail_last_injected_text.setText(
            self._display_value(last_result.get("injected_text"))
        )
        self.detail_tail_text.setText(self._display_value(tail))
        self.detail_branch_text.setText(self._lines_value(preview_sections["branch"]))
        self.detail_streams_text.setText(self._lines_value(preview_sections["streams"]))
        self.detail_windows_text.setText(self._lines_value(preview_sections["windows"]))

    def _scroll_wrap(self, label: QLabel, height: int) -> QScrollArea:
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setContentsMargins(8, 6, 8, 6)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        area.setFixedHeight(height)
        area.setWidget(label)
        return area

    def _restore_position(self) -> None:
        try:
            data = json.loads(self.position_path.read_text(encoding="utf-8"))
            x = int(data.get("x", 0))
            y = int(data.get("y", 0))
            self.move(x, y)
            return
        except Exception:
            pass

        screen = QApplication.primaryScreen()
        if screen is None:
            self.move(40, 40)
            return
        geom = screen.availableGeometry()
        self.move(
            max(0, geom.right() - self.width() - 18),
            max(0, geom.bottom() - self.height() - 48),
        )

    def _save_position(self) -> None:
        try:
            self.position_path.parent.mkdir(parents=True, exist_ok=True)
            self.position_path.write_text(
                json.dumps({"x": self.x(), "y": self.y()}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def update_status(
        self,
        visual: VisualState,
        status: dict[str, Any] | None,
        status_error: str,
    ) -> None:
        self._last_visual = visual
        self._last_status = status
        self._last_status_error = status_error

        display = status.get("dictation_display", {}) if isinstance(status, dict) else {}
        if not isinstance(display, dict):
            display = {}

        if self.details_expanded:
            self._refresh_details()

        source = str(display.get("source") or "offline")
        profile = str(status.get("profile") or "unknown") if isinstance(status, dict) else "offline"
        state = str(display.get("state") or visual.label)
        gate = (
            str(display.get("audio_gate_label") or status.get("audio_gate_label") or "offline")
            if isinstance(status, dict)
            else "offline"
        )
        committed = str(display.get("committed_text") or display.get("final_text") or "")
        partial = str(display.get("partial_text") or "")
        if not committed and isinstance(status, dict) and not status.get("recording"):
            committed = str(status.get("last_commit_preview") or "")

        gate_status = self.owner._voice_gate_status()
        level_value = int(round(float(gate_status["level_ratio"]) * 100))

        self.state_label.setText(f"{visual.label} / {state}")
        self.profile_label.setText(f"profile={profile}")
        self._syncing_profile_combo = True
        profile_index = self.profile_combo.findText(profile)
        if profile_index < 0 and profile:
            self.profile_combo.addItem(profile)
            profile_index = self.profile_combo.findText(profile)
        if profile_index >= 0:
            self.profile_combo.setCurrentIndex(profile_index)
        self._syncing_profile_combo = False
        self.source_label.setText(f"source={source}")
        self.gate_label.setText(gate if not status_error else f"error={status_error}")
        self.committed_text.setText(committed or " ")
        self.partial_text.setText(partial or " ")
        self.level_bar.setValue(level_value)
        self.status_dot.setStyleSheet(
            f"border-radius: 5px; background-color: {visual.color};"
        )

        local_recording = bool(status.get("recording")) if status else False
        busy = (
            local_recording or bool(status.get("external_session_active"))
            if status
            else False
        )
        online = status is not None
        mode = str(status.get("mode", "ptt")).lower() if status else "ptt"
        self.record_button.setEnabled(online and not busy)
        self.commit_button.setEnabled(online and local_recording)
        self.cancel_button.setEnabled(online and local_recording)
        self.mode_button.setEnabled(online and not busy)
        self.mode_button.setText("PTT" if mode == "live" else "Live")
        self.profile_combo.setEnabled(online and not busy)

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event_global_pos(event) - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event_global_pos(event) - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if self._drag_offset is not None:
            self._drag_offset = None
            self._save_position()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def closeEvent(self, event: Any) -> None:
        self.hide()
        self.owner.action_toggle_hud.setText("Show HUD")
        event.ignore()


class DictationTray:
    def __init__(self, config_path: Path, poll_ms: int):
        self.config_path = config_path.resolve()
        self.socket_path = load_socket_path(self.config_path)
        self.poll_ms = max(150, poll_ms)
        self.status: dict[str, Any] | None = None
        self.status_error = ""
        self._icon_cache: dict[str, QIcon] = {}

        self.app = QApplication([sys.argv[0]])
        self.app.setApplicationName("STT Tray")
        self.app.setQuitOnLastWindowClosed(False)

        if not QSystemTrayIcon.isSystemTrayAvailable():
            raise RuntimeError("System tray is not available in this desktop session.")

        self.tray = QSystemTrayIcon()
        self.menu = QMenu()
        self.tray.setContextMenu(self.menu)

        self.action_record = QAction("Record / Start")
        self.action_commit = QAction("Commit")
        self.action_discard = QAction("Discard")
        self.action_toggle_live = QAction("Toggle Live Mode")
        self.action_refresh = QAction("Refresh")
        self.action_toggle_hud = QAction("Hide HUD")
        self.action_quit = QAction("Quit STT Tray")

        self.action_record.triggered.connect(lambda: self._send_control("start"))
        self.action_commit.triggered.connect(lambda: self._send_control("stop_commit"))
        self.action_discard.triggered.connect(lambda: self._send_control("stop_cancel"))
        self.action_toggle_live.triggered.connect(lambda: self._send_control("toggle_live"))
        self.action_refresh.triggered.connect(self.refresh_now)
        self.action_toggle_hud.triggered.connect(self._toggle_hud)
        self.action_quit.triggered.connect(self.app.quit)

        self.menu.addAction(self.action_record)
        self.menu.addAction(self.action_commit)
        self.menu.addAction(self.action_discard)
        self.menu.addSeparator()
        self.menu.addAction(self.action_toggle_live)
        self.menu.addAction(self.action_toggle_hud)
        self.menu.addAction(self.action_refresh)
        self.menu.addSeparator()
        self.menu.addAction(self.action_quit)

        self.hud = FloatingHud(self)
        self.hud.show()

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh_now)
        self.timer.start(self.poll_ms)

        self.refresh_now()
        self.tray.setVisible(True)

    def run(self) -> int:
        self.tray.show()
        return int(self.app.exec())

    def _profile_names(self) -> list[str]:
        profile_dir = self.config_path.parent / "profiles"
        names: list[str] = []
        try:
            for path in sorted(profile_dir.glob("*.toml")):
                name = path.stem
                if name == "superfast":
                    continue
                names.append(name)
        except OSError:
            pass
        return names or ["default", "obsidian"]

    def _send_control(self, cmd: str, extra: dict[str, Any] | None = None) -> None:
        payload = {"cmd": cmd}
        if extra:
            payload.update(extra)
        try:
            response = send_command(self.socket_path, payload, timeout=1.5)
        except Exception as exc:
            self.status_error = str(exc)
            self._show_message("STT", f"{cmd} failed: {exc}", critical=True)
            self.refresh_now()
            return

        if not response.get("ok"):
            error = str(response.get("error", "unknown error"))
            self.status_error = error
            self._show_message("STT", f"{cmd} failed: {error}", critical=True)
        else:
            self.status_error = ""
        self.refresh_now()

    def _show_message(self, title: str, body: str, critical: bool = False) -> None:
        icon = (
            QSystemTrayIcon.MessageIcon.Critical
            if critical
            else QSystemTrayIcon.MessageIcon.Information
        )
        self.tray.showMessage(title, body, icon, 2500)

    def refresh_now(self) -> None:
        self.status, fetch_error = self._fetch_status()
        if fetch_error:
            self.status_error = fetch_error
        elif self.status_error and self.status is not None:
            self.status_error = ""

        visual = self._resolve_visual_state()
        self.tray.setIcon(self._icon_for_state(visual))
        self.tray.setToolTip(self._build_tooltip(visual))
        self._sync_actions()
        self.hud.update_status(visual, self.status, self.status_error)

    def _toggle_hud(self) -> None:
        if self.hud.isVisible():
            self.hud.hide()
            self.action_toggle_hud.setText("Show HUD")
            return
        self.hud.show()
        self.hud.raise_()
        self.action_toggle_hud.setText("Hide HUD")

    def _fetch_status(self) -> tuple[dict[str, Any] | None, str]:
        try:
            response = send_command(self.socket_path, {"cmd": "status"}, timeout=1.2)
        except FileNotFoundError:
            return None, f"socket not found: {self.socket_path}"
        except (ConnectionRefusedError, socket.timeout, OSError) as exc:
            return None, str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            return None, str(exc)

        if not response.get("ok"):
            return None, str(response.get("error", "status command failed"))

        status = response.get("status")
        if not isinstance(status, dict):
            return None, "invalid status payload from daemon"
        return status, ""

    def _resolve_visual_state(self) -> VisualState:
        if self.status is None:
            return STATE_OFFLINE
        if str(self.status.get("last_error", "")).strip():
            return STATE_ERROR

        activity = str(self.status.get("activity_state", "")).strip().lower()
        try:
            activity_age = time.time() - float(self.status.get("activity_updated_at", 0.0))
        except (TypeError, ValueError):
            activity_age = 999.0

        if (
            bool(self.status.get("recording"))
            or bool(self.status.get("external_session_active"))
            or activity == "recording"
        ):
            return (
                STATE_RECORDING_ACCEPTING
                if self._voice_gate_status()["accepting"]
                else STATE_RECORDING_REJECTING
            )
        if activity == "finalizing":
            return STATE_FINALIZING
        if activity_age <= 2.5:
            if activity == "committed":
                return STATE_COMMITTED
            if activity in {"no_voice", "no_audio"}:
                return STATE_NO_VOICE
            if activity == "cancelled":
                return STATE_CANCELLED
        if str(self.status.get("mode", "")).lower() == "live":
            return STATE_LIVE
        return STATE_IDLE

    def _build_tooltip(self, visual: VisualState) -> str:
        lines = [f"STT: {visual.label}"]
        if self.status is not None:
            mode = self.status.get("mode", "?")
            profile = self.status.get("profile", "?")
            recording = bool(self.status.get("recording")) or bool(
                self.status.get("external_session_active")
            )
            input_target = self.status.get("input_target") or "<default>"
            activity = self.status.get("activity_state", "idle")
            gate = self._voice_gate_status()
            lines.append(f"mode={mode} profile={profile}")
            lines.append(f"recording={recording} activity={activity}")
            if recording:
                lines.append(
                    f"input={input_target} level={gate['level']:.2f}% peak={gate['peak']:.2f}%"
                )
                lines.append(
                    f"gate={gate['label']} "
                    f"voice={gate['voice_chunks']}/{gate['voice_min']} "
                    f"strong={gate['strong_chunks']}/{gate['strong_min']}"
                )
            else:
                lines.append(f"input={input_target}")

        if self.status_error:
            lines.append(f"error: {self.status_error}")

        return "\n".join(lines)

    def _sync_actions(self) -> None:
        local_recording = bool(self.status.get("recording")) if self.status else False
        busy = (
            local_recording or bool(self.status.get("external_session_active"))
            if self.status
            else False
        )
        online = self.status is not None
        mode = str(self.status.get("mode", "ptt")).lower() if self.status else "ptt"

        self.action_record.setEnabled(online and not busy)
        self.action_commit.setEnabled(online and local_recording)
        self.action_discard.setEnabled(online and local_recording)
        self.action_toggle_live.setEnabled(online and not busy)
        self.action_toggle_live.setText(
            "Switch To PTT Mode" if mode == "live" else "Switch To Live Mode"
        )

    def _status_float(self, key: str, default: float = 0.0) -> float:
        if self.status is None:
            return default
        try:
            return float(self.status.get(key, default))
        except (TypeError, ValueError):
            return default

    def _status_int(self, key: str, default: int = 0) -> int:
        if self.status is None:
            return default
        try:
            return int(self.status.get(key, default))
        except (TypeError, ValueError):
            return default

    def _voice_gate_status(self) -> dict[str, Any]:
        accepting = bool(self.status.get("audio_gate_accepting")) if self.status else False
        if self.status and "audio_gate_accepting" not in self.status:
            accepting = bool(self.status.get("session_voice_seen"))

        label = "accepting voice" if accepting else "rejecting silence"
        if self.status:
            label = str(self.status.get("audio_gate_label") or label)

        level = self._status_float("audio_level_percent")
        peak = self._status_float("session_peak_level_percent")
        display_max = max(1.0, self._status_float("audio_level_display_max_percent", 12.0))
        return {
            "accepting": accepting,
            "label": label,
            "level": level,
            "peak": peak,
            "level_ratio": max(0.0, min(1.0, level / display_max)),
            "voice_chunks": self._status_int("session_voice_chunk_count"),
            "voice_min": self._status_int("audio_level_ready_min_chunks", 1),
            "strong_chunks": self._status_int("session_strong_voice_chunk_count"),
            "strong_min": self._status_int("audio_level_ready_min_strong_chunks", 1),
        }

    def _icon_for_state(self, visual: VisualState) -> QIcon:
        cache_key = visual.key
        gate = self._voice_gate_status()
        if visual.key.startswith("recording"):
            level_bucket = int(round(gate["level_ratio"] * 7))
            cache_key = f"{visual.key}:{level_bucket}:{int(gate['accepting'])}"

        cached = self._icon_cache.get(cache_key)
        if cached is not None:
            return cached

        size = 24
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        pen = QPen(QColor("#0f172a"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(QColor(visual.color))
        painter.drawRoundedRect(QRectF(4.0, 4.0, 16.0, 16.0), 5.0, 5.0)

        if visual.key.startswith("recording"):
            self._draw_recording_overlay(painter, gate)
        elif visual.key == "finalizing":
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawArc(QRectF(8.0, 8.0, 8.0, 8.0), 30 * 16, 280 * 16)
        elif visual.key == "committed":
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(8, 12, 11, 15)
            painter.drawLine(11, 15, 16, 9)
        elif visual.key in {"no_voice", "cancelled"}:
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(8, 8, 16, 16)
            painter.drawLine(16, 8, 8, 16)
        elif visual.key == "error":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#7c2d12"))
            painter.drawRect(QRectF(11.0, 8.0, 2.0, 6.0))
            painter.drawEllipse(QRectF(11.0, 15.5, 2.0, 2.0))

        painter.end()

        icon = QIcon(pixmap)
        self._icon_cache[cache_key] = icon
        return icon

    def _draw_recording_overlay(self, painter: QPainter, gate: dict[str, Any]) -> None:
        meter_top = 7.0
        meter_height = 10.0
        fill_height = max(1.0, meter_height * float(gate["level_ratio"]))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#7f1d1d" if gate["accepting"] else "#7c2d12"))
        painter.drawRoundedRect(QRectF(6.0, meter_top, 3.0, meter_height), 1.0, 1.0)
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(
            QRectF(6.0, meter_top + meter_height - fill_height, 3.0, fill_height),
            1.0,
            1.0,
        )

        if gate["accepting"]:
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.drawLine(11, 13, 13, 15)
            painter.drawLine(13, 15, 18, 9)
            return

        painter.setPen(QPen(QColor("#ffffff"), 2))
        painter.drawLine(12, 9, 18, 15)
        painter.drawLine(18, 9, 12, 15)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="STT system tray status indicator")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument(
        "--poll-ms",
        type=int,
        default=700,
        help="Status poll interval in milliseconds (default: 700)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        tray = DictationTray(args.config, args.poll_ms)
    except Exception as exc:
        print(f"Failed to initialize tray: {exc}", file=sys.stderr)
        return 1
    return tray.run()


if __name__ == "__main__":
    raise SystemExit(main())
