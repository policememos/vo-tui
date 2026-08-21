#!/usr/bin/env python3
"""A small terminal UI for the on-device `vo` live translation CLI.

It starts `vo --json`, then renders finalized source and translated chunks in
side-by-side panels. No API key or network connection is used at runtime.
"""

from __future__ import annotations

import argparse
import curses
import json
import os
import queue
import signal
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from textwrap import wrap
from typing import Deque, Optional


@dataclass
class Segment:
    timestamp: str
    channel: str
    source: str
    translation: str


class VoProcess:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.process: Optional[subprocess.Popen[str]] = None
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.running:
            return

        command = [
            self.args.vo,
            "--json",
            "--src",
            self.args.source,
            "--dst",
            self.args.target,
        ]
        if not self.args.include_mic:
            command.append("--no-mic")

        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            # Give vo (and anything it starts) its own process group so the
            # TUI can reliably clean it up on every exit path.
            start_new_session=True,
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        threading.Thread(target=self._read, args=("json", self.process.stdout), daemon=True).start()
        threading.Thread(target=self._read, args=("status", self.process.stderr), daemon=True).start()

    def stop(self) -> None:
        if not self.process:
            return
        process = self.process
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            pass
        # The parent can exit before a helper it spawned.  Kill any remaining
        # processes in the dedicated group as the final cleanup step.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process = None

    def _read(self, kind: str, stream: object) -> None:
        for line in stream:  # type: ignore[union-attr]
            text = line.strip()
            if text:
                self.events.put((kind, text))


class BrewInstaller:
    """Runs the optional Homebrew setup without blocking the TUI."""

    command = ["brew", "install", "k1LoW/tap/vo"]

    def __init__(self) -> None:
        self.process: Optional[subprocess.Popen[str]] = None
        self.events: queue.Queue[str] = queue.Queue()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.running:
            return
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        assert self.process.stdout is not None
        threading.Thread(target=self._read, args=(self.process.stdout,), daemon=True).start()

    def stop(self) -> None:
        if not self.process:
            return
        process = self.process
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process = None

    def _read(self, stream: object) -> None:
        for line in stream:  # type: ignore[union-attr]
            text = line.strip()
            if text:
                self.events.put(text)


class Tui:
    def __init__(self, screen: "curses._CursesWindow", args: argparse.Namespace) -> None:
        self.screen = screen
        self.args = args
        self.vo = VoProcess(args)
        self.installer = BrewInstaller()
        self.vo_available = self._vo_is_available(args.vo)
        self.install_message = ""
        self.segments: Deque[Segment] = deque(maxlen=args.max_lines)
        self.status = "Ready — press Space to start" if self.vo_available else "vo is not installed"
        self.scroll = 0
        self.follow = True
        self.last_draw = 0.0
        self._startup_message_until = 0.0
        self._feedback_message_until = 0.0
        self._saved_path: Optional[Path] = None
        # stderr is retained for a launch failure so macOS model/setup advice
        # from vo is shown in a readable dialog instead of disappearing into
        # the one-line status bar.
        self._vo_status_lines: list[str] = []
        self._vo_error_lines: Optional[list[str]] = None
        # (left x, right x, y) in SGR mouse coordinates for the setup URL.
        self._setup_link_hitbox: Optional[tuple[int, int, int]] = None
        self._input_buffer = bytearray()
        self._escape_since: Optional[float] = None
        self._input_fd: Optional[int] = None
        self._input_was_blocking: Optional[bool] = None

    def run(self) -> None:
        curses.curs_set(0)
        self.screen.nodelay(True)
        self.screen.keypad(False)
        # Python's macOS curses binding loses the wheel-down event.  Read the
        # standard SGR mouse protocol directly instead of asking curses to
        # translate it into BUTTON4/BUTTON5 constants.
        try:
            curses.mousemask(0)
        except curses.error:
            pass
        self._configure_raw_input()
        self._enable_mouse_reporting()
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_RED, -1)

        try:
            while True:
                self._drain_events()
                self._drain_installer_events()
                if self._poll_input():
                    break
                if time.monotonic() - self.last_draw > 0.025:
                    self._draw()
                    self.last_draw = time.monotonic()
                # Keep keyboard and wheel input responsive without busy-looping.
                time.sleep(0.005)
        finally:
            self._disable_mouse_reporting()
            self._restore_input_mode()
            self.vo.stop()
            self.installer.stop()

    def _configure_raw_input(self) -> None:
        try:
            self._input_fd = sys.stdin.fileno()
            self._input_was_blocking = os.get_blocking(self._input_fd)
            os.set_blocking(self._input_fd, False)
        except (OSError, AttributeError):
            self._input_fd = None

    def _restore_input_mode(self) -> None:
        if self._input_fd is None or self._input_was_blocking is None:
            return
        try:
            os.set_blocking(self._input_fd, self._input_was_blocking)
        except OSError:
            pass

    @staticmethod
    def _enable_mouse_reporting() -> None:
        # 1000 enables button events; 1006 uses the unambiguous SGR format.
        sys.stdout.write("\x1b[?1000h\x1b[?1006h")
        sys.stdout.flush()

    @staticmethod
    def _disable_mouse_reporting() -> None:
        sys.stdout.write("\x1b[?1006l\x1b[?1000l")
        sys.stdout.flush()

    def _poll_input(self) -> bool:
        """Consume keyboard and SGR wheel events without ncurses mouse loss."""
        if self._input_fd is not None:
            try:
                while True:
                    chunk = os.read(self._input_fd, 4096)
                    if not chunk:
                        break
                    self._input_buffer.extend(chunk)
                    if len(chunk) < 4096:
                        break
            except BlockingIOError:
                pass
            except OSError as error:
                self.status = f"Input error: {error}"
                self._input_fd = None
        return self._consume_input_buffer()

    def _consume_input_buffer(self) -> bool:
        while self._input_buffer:
            if self._input_buffer[0] == 0x1B:
                result = self._consume_escape_sequence()
                if result is None:
                    return False
                if result:
                    return True
                continue

            character_length = self._utf8_character_length(self._input_buffer[0])
            if len(self._input_buffer) < character_length:
                return False
            raw = bytes(self._input_buffer[:character_length])
            del self._input_buffer[:character_length]
            try:
                key = ord(raw.decode("utf-8"))
            except UnicodeDecodeError:
                continue
            if self._handle_key(key):
                return True
        return False

    def _consume_escape_sequence(self) -> Optional[bool]:
        data = self._input_buffer
        # SGR protocol: ESC [ < button ; x ; y M.  Wheel up/down are 64/65.
        if data.startswith(b"\x1b[<"):
            end = next((i for i, byte in enumerate(data[3:], 3) if byte in (ord("M"), ord("m"))), None)
            if end is None:
                return None
            payload = bytes(data[3:end]).split(b";")
            event_kind = data[end]
            del data[: end + 1]
            if len(payload) == 3:
                try:
                    button = int(payload[0])
                    x = int(payload[1])
                    y = int(payload[2])
                except ValueError:
                    return False
                if button & 64:
                    self._scroll_by(-1 if button & 1 else 1)
                elif event_kind == ord("M") and (button & 3) == 0:
                    self._handle_saved_path_click(x, y, button)
                    self._handle_setup_link_click(x, y, button)
            self._escape_since = None
            return False

        # Legacy X10 protocol: ESC [ M followed by button, x, y bytes.
        if data.startswith(b"\x1b[M"):
            if len(data) < 6:
                return None
            button = data[3] - 32
            x = data[4] - 32
            y = data[5] - 32
            del data[:6]
            if button & 64:
                self._scroll_by(-1 if button & 1 else 1)
            elif (button & 3) == 0:
                self._handle_saved_path_click(x, y, button)
                self._handle_setup_link_click(x, y, button)
            self._escape_since = None
            return False

        key_sequences = (
            (b"\x1b[A", curses.KEY_UP), (b"\x1bOA", curses.KEY_UP),
            (b"\x1b[B", curses.KEY_DOWN), (b"\x1bOB", curses.KEY_DOWN),
            (b"\x1b[H", curses.KEY_HOME), (b"\x1bOH", curses.KEY_HOME),
            (b"\x1b[F", curses.KEY_END), (b"\x1bOF", curses.KEY_END),
            (b"\x1b[1~", curses.KEY_HOME), (b"\x1b[4~", curses.KEY_END),
        )
        for sequence, key in key_sequences:
            if data.startswith(sequence):
                del data[: len(sequence)]
                self._escape_since = None
                return self._handle_key(key)
            if sequence.startswith(data):
                return None

        # A lone Escape is still a quit shortcut, but wait briefly so it is
        # not mistaken for the beginning of an arrow or mouse sequence.
        if len(data) == 1:
            if self._escape_since is None:
                self._escape_since = time.monotonic()
                return None
            if time.monotonic() - self._escape_since < 0.04:
                return None
        del data[0]
        self._escape_since = None
        return self._handle_key(27)

    @staticmethod
    def _utf8_character_length(first_byte: int) -> int:
        if first_byte < 0x80:
            return 1
        if first_byte < 0xE0:
            return 2
        if first_byte < 0xF0:
            return 3
        return 4

    @staticmethod
    def _vo_is_available(candidate: str) -> bool:
        if "/" in candidate:
            path = Path(candidate).expanduser()
            return path.is_file() and os.access(path, os.X_OK)
        return shutil.which(candidate) is not None

    def _start_auto_setup(self) -> None:
        self.install_message = "Starting Homebrew…"
        self.status = "Setting up vo with Homebrew"
        try:
            self.installer.start()
        except FileNotFoundError:
            self.install_message = "Homebrew was not found. Install it first, or use the GitHub link below."
        except OSError as error:
            self.install_message = f"Could not start Homebrew: {error}"
        self.last_draw = 0

    def _drain_installer_events(self) -> None:
        changed = False
        while True:
            try:
                self.install_message = self.installer.events.get_nowait()
                changed = True
            except queue.Empty:
                break

        if self.installer.process and self.installer.process.poll() is not None:
            code = self.installer.process.returncode
            self.installer.process = None
            if code == 0 and self._vo_is_available("vo"):
                self.args.vo = shutil.which("vo") or "vo"
                self.vo_available = True
                self.install_message = ""
                self.status = "vo installed — press Space to start"
            else:
                self.install_message = (
                    "Automatic setup failed. Use the GitHub link below, then press Space to retry."
                )
            changed = True
        if changed:
            self.last_draw = 0

    def _drain_events(self) -> None:
        changed = False
        while True:
            try:
                kind, payload = self.vo.events.get_nowait()
            except queue.Empty:
                break

            if kind == "status":
                self._vo_status_lines.append(payload)
                self._vo_status_lines = self._vo_status_lines[-24:]
                if self._vo_error_lines is not None:
                    self._vo_error_lines.append(payload)
                self.status = payload
                changed = True
                continue

            try:
                event = json.loads(payload)
                src = event.get("src") or {}
                dst = event.get("dst") or {}
                source = str(src.get("text") or "")
                translation = str(dst.get("text") or "")
                if not source:
                    continue
                timestamp = str(event.get("timestamp") or "")
                timestamp = self._short_time(timestamp)
                channel = str(event.get("channel") or "speaker")
                # scroll is measured from the newest row.  Preserve the same
                # history on screen while new segments arrive in scroll mode.
                previous_limit = self._max_scroll()
                self.segments.append(Segment(timestamp, channel, source, translation))
                if self.follow:
                    self.scroll = 0
                else:
                    self.scroll += self._max_scroll() - previous_limit
                    self._clamp_scroll()
                changed = True
            except (TypeError, ValueError, json.JSONDecodeError):
                self.status = f"Could not parse vo output: {payload[:90]}"
                changed = True

        if self.vo.process and self.vo.process.poll() is not None:
            code = self.vo.process.returncode
            if code:
                self._vo_error_lines = self._vo_status_lines[:] or [
                    f"vo exited with code {code}."
                ]
                summary = next(
                    (line for line in self._vo_error_lines if line.startswith("Error:")),
                    self._vo_error_lines[-1],
                )
                self.status = summary
            else:
                self.status = f"vo stopped (exit {code}). Press Space to run again."
            self._startup_message_until = 0.0
            self.vo.process = None
            changed = True
        if changed:
            self.last_draw = 0

    def _handle_key(self, key: int) -> bool:
        # Keep shortcuts usable when the active macOS input source is Russian.
        # These are the Cyrillic characters produced by the same physical keys.
        # q/й, Escape and Ctrl+C all leave through run()'s finally block,
        # which terminates the complete vo process group.
        if key in (*self._keys("q", "й"), 3, 27):
            return True
        if not self.vo_available:
            if key == ord(" ") and not self.installer.running:
                self._start_auto_setup()
            return False
        if key == ord(" "):
            if self.vo.running:
                self.vo.stop()
                self.status = ""
                self._startup_message_until = 0.0
                self._feedback_message_until = 0.0
            else:
                self.status = "Waiting for vo… macOS may ask for permissions."
                self._startup_message_until = time.monotonic() + 1.2
                self._feedback_message_until = 0.0
                self._vo_status_lines.clear()
                self._vo_error_lines = None
                try:
                    self.vo.start()
                except FileNotFoundError:
                    self.vo_available = False
                    self.status = f"Cannot find vo at: {self.args.vo}"
                except OSError as error:
                    self.status = f"Cannot start vo: {error}"
                    self._vo_error_lines = [self.status]
            return False
        if key in self._keys("c", "с"):
            self.segments.clear()
            self.scroll = 0
            self.follow = True
            self.status = "Transcript cleared"
        elif key in (curses.KEY_UP, *self._keys("k", "л")):
            self._scroll_by(1)
        elif key in (curses.KEY_DOWN, *self._keys("j", "о")):
            self._scroll_by(-1)
        elif key == curses.KEY_HOME:
            self.scroll = self._max_scroll()
            self.follow = self.scroll == 0
        elif key in (curses.KEY_END, *self._keys("g", "п")):
            self.scroll = 0
            self.follow = True
        elif key in self._keys("s", "ы"):
            self._save_transcript()
        return False

    @staticmethod
    def _keys(latin: str, cyrillic: str) -> tuple[int, ...]:
        """Return both cases for a matching Latin/Russian keyboard key."""
        if len(latin) != 1 or len(cyrillic) != 1:
            raise ValueError("_keys expects one Latin and one Cyrillic character")
        return tuple(ord(character) for character in (
            latin.lower(), latin.upper(), cyrillic.lower(), cyrillic.upper(),
        ))

    def _scroll_by(self, delta: int) -> None:
        """Move only as far as there are wrapped transcript rows to show."""
        limit = self._max_scroll()
        self.scroll = max(0, min(limit, self.scroll + delta))
        self.follow = self.scroll == 0

    def _handle_saved_path_click(self, x: int, y: int, modifiers: int) -> None:
        """Copy the saved path, or reveal it with Ctrl+click in Finder."""
        if self._saved_path is None:
            return
        height, _width = self.screen.getmaxyx()
        # SGR mouse coordinates are one-based.  Accept the Saved line and its
        # instruction line immediately below it.
        if y not in (height - 1, height):
            return
        try:
            if modifiers & 16:  # Ctrl modifier in SGR mouse protocol.
                subprocess.Popen(
                    ["open", "-R", str(self._saved_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.run(
                    ["pbcopy"],
                    input=str(self._saved_path),
                    text=True,
                    check=True,
                )
        except (OSError, subprocess.SubprocessError) as error:
            self.status = f"Could not handle saved path: {error}"
            self._feedback_message_until = time.monotonic() + 5

    def _handle_setup_link_click(self, x: int, y: int, modifiers: int) -> None:
        """Open the vo project page when its setup link is clicked."""
        hitbox = self._setup_link_hitbox
        if hitbox is None:
            return
        left, right, row = hitbox
        if y != row or not left <= x <= right:
            return
        try:
            subprocess.Popen(
                ["open", "https://github.com/k1LoW/vo"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            self.status = f"Could not open the vo link: {error}"
            self._feedback_message_until = time.monotonic() + 5

    def _max_scroll(self) -> int:
        height, width = self.screen.getmaxyx()
        if height < 8 or width < 48:
            return 0
        # Footer reserves: divider, controls, status bar, Saved path and its
        # action hint.  The Saved block is deliberately separate from status.
        visible_rows = max(1, (height - 5) - 4)
        return max(0, len(self._all_rows(width // 2, width)) - visible_rows)

    def _clamp_scroll(self) -> None:
        self.scroll = min(self.scroll, self._max_scroll())
        self.follow = self.scroll == 0

    def _save_transcript(self) -> None:
        if not self.segments:
            self.status = "Nothing to save yet"
            self._feedback_message_until = time.monotonic() + 4
            return
        try:
            self.args.save_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            path = self.args.save_dir / f"vo-tui_{stamp}.md"
            lines = [
                f"# vo-tui transcript — {self.args.source} → {self.args.target}",
                "",
            ]
            for segment in self.segments:
                lines.extend([
                    f"## {segment.timestamp} · {segment.channel}",
                    "",
                    f"**Original:** {segment.source}",
                    "",
                    f"**Translation:** {segment.translation or '—'}",
                    "",
                ])
            path.write_text("\n".join(lines), encoding="utf-8")
            self._saved_path = path.resolve()
            # Saved has its own block below the status bar; do not overwrite
            # the application's current state text with a path notification.
            self._feedback_message_until = 0.0
        except OSError as error:
            self.status = f"Could not save transcript: {error}"
            self._feedback_message_until = time.monotonic() + 5

    def _statusbar_text(self, now: float) -> tuple[str, int]:
        """Return the persistent, explicit application status-bar content."""
        if self.installer.running:
            return "Status: SETTING UP — Installing vo with Homebrew", self._color(3) | curses.A_BOLD
        if not self.vo_available:
            return "Status: SETUP REQUIRED — vo is not installed", self._color(4) | curses.A_BOLD
        if self.vo.running:
            visible_until = max(self._startup_message_until, self._feedback_message_until)
            if now < visible_until and self.status:
                return f"Status: WORKING — {self.status}", self._color(2) | curses.A_BOLD
            return "Status: WORKING", self._color(2) | curses.A_BOLD
        if self.status.startswith(("Cannot ", "Could not ", "Error:", "vo stopped (exit")):
            return f"Status: ERROR — {self.status}", self._color(4) | curses.A_BOLD
        if self.status:
            return f"Status: STOPPED — {self.status}", self._color(3) | curses.A_BOLD
        return "Status: STOPPED", self._color(3) | curses.A_BOLD

    def _draw(self) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 8 or width < 48:
            self._safe_add(0, 0, "Terminal is too small — resize to at least 48×8.", curses.A_BOLD)
            self.screen.refresh()
            return

        state = "● LIVE" if self.vo.running else "○ STOPPED"
        state_attr = self._color(2 if self.vo.running else 3) | curses.A_BOLD
        title = f" vo-tui  ·  {self.args.source} → {self.args.target}  "
        self._safe_add(0, 1, title, curses.A_BOLD)
        space_hint = "Press Space"
        if width >= 72:
            hint_x = max(len(title) + 2, width - len(space_hint) - len(state) - 4)
            self._safe_add(0, hint_x, space_hint, curses.A_DIM)
        self._safe_add(0, max(1, width - len(state) - 2), state, state_attr)
        self._hline(1, width)

        split = width // 2
        left_title = "ENGLISH — transcript"
        right_title = "РУССКИЙ — перевод"
        left_title_x = max(1, (split - len(left_title)) // 2)
        right_panel_width = width - split - 1
        right_title_x = split + 1 + max(0, (right_panel_width - len(right_title)) // 2)
        self._safe_add(2, left_title_x, left_title, self._color(1) | curses.A_BOLD)
        self._safe_add(2, right_title_x, right_title, self._color(2) | curses.A_BOLD)
        self._safe_add(3, split, "│", curses.A_DIM)

        body_top, body_bottom = 4, height - 5
        visible_rows = max(1, body_bottom - body_top)
        self._clamp_scroll()
        rows = self._render_rows(split, width, visible_rows)
        for index, row in enumerate(rows):
            y = body_top + index
            if y >= body_bottom:
                break
            timestamp, left, right, attr = row
            # A separate narrow time column makes timestamps less visually
            # prominent while keeping every spoken line easy to scan.
            left_x = 1
            if timestamp is not None:
                if timestamp:
                    self._safe_add(y, left_x, timestamp, curses.A_DIM)
                left_x += 9  # HH:MM:SS plus one space
            self._safe_add(y, left_x, left[: max(0, split - left_x - 1)], attr)
            self._safe_add(y, split, "│", curses.A_DIM)
            self._safe_add(y, split + 4, right[: max(0, width - split - 5)], attr)

        self._hline(height - 5, width)
        marker = "follow" if self.follow else f"scroll +{self.scroll}"
        help_text = "↑↓/wheel · End/G scroll to end · S save · C clear · Q quit"
        self._safe_add(height - 4, 1, help_text, curses.A_DIM)
        status, status_attr = self._statusbar_text(time.monotonic())
        # Status bar: application state, never replaced by Saved feedback.
        self._safe_add(height - 3, 1, status[: width - 4], status_attr)
        if self._saved_path is not None:
            self._safe_add(height - 2, 1, f"Saved: {self._saved_path}"[: width - 4], curses.A_DIM)
            self._safe_add(height - 1, 1, "Click on: copy · Ctrl+click: Finder", curses.A_DIM)
        self._safe_add(height - 4, max(1, width - len(marker) - 2), marker, curses.A_DIM)
        if self._vo_error_lines:
            self._draw_vo_error_dialog(height, width)
        elif not self.vo_available:
            self._draw_setup_dialog(height, width)
        self.screen.refresh()

    def _draw_vo_error_dialog(self, height: int, width: int) -> None:
        """Show launch errors from vo, especially unavailable Apple models."""
        lines: list[tuple[str, int]] = []
        for raw in self._vo_error_lines or []:
            attr = curses.A_NORMAL
            if raw.startswith("Error:"):
                attr = self._color(4) | curses.A_BOLD
            elif raw.startswith("vo ") or raw.startswith("speaker"):
                attr = curses.A_DIM
            lines.append((raw, attr))
        if any("Translation model" in line and "not installed" in line
               for line in self._vo_error_lines or []):
            # Apple's error is useful but English-only.  Keep the exact
            # command output above, then give the user the same resolution in
            # Russian, including the offline-model switch in macOS.
            lines.extend([
                ("", curses.A_NORMAL),
                ("Как установить языковые пакеты:", self._color(1) | curses.A_BOLD),
                ("Откройте Системные настройки → Основные → Язык и регион", curses.A_NORMAL),
                ("→ Языки для перевода.", curses.A_NORMAL),
                ("Скачайте English (US) и Русский, затем включите", curses.A_NORMAL),
                ("«Режим локального перевода» для офлайн-перевода.", curses.A_NORMAL),
            ])
        lines.extend([
            ("", curses.A_NORMAL),
            ("Press Space to retry · Q to quit", curses.A_DIM),
        ])

        box_width = min(width - 4, 86)
        inner_width = max(16, box_width - 4)
        display_lines: list[tuple[str, int]] = [("", curses.A_NORMAL)]
        for line, attr in lines:
            display_lines.extend((part, attr) for part in (wrap(line, inner_width) or [""]))
        box_height = len(display_lines) + 3
        x = max(1, (width - box_width) // 2)
        y = max(2, (height - box_height) // 2)
        border_attr = self._color(4) | curses.A_BOLD
        title = "vo error"
        title_x = x + max(2, (box_width - len(title)) // 2)
        self._safe_add(y, x, "┌" + "─" * (box_width - 2) + "┐", border_attr)
        self._safe_add(y + 1, x, "│", border_attr)
        self._safe_add(y + 1, title_x, title, border_attr)
        self._safe_add(y + 1, x + box_width - 1, "│", border_attr)
        for index, (line, attr) in enumerate(display_lines, 2):
            row = y + index
            self._safe_add(row, x, "│", border_attr)
            line_x = x + max(2, (box_width - len(line)) // 2)
            self._safe_add(row, line_x, line, attr)
            self._safe_add(row, x + box_width - 1, "│", border_attr)
        bottom = y + box_height - 1
        self._safe_add(bottom, x, "└" + "─" * (box_width - 2) + "┘", border_attr)

    def _draw_setup_dialog(self, height: int, width: int) -> None:
        """Centered setup dialog shown until the required vo command exists."""
        self._setup_link_hitbox = None
        command = "brew install k1LoW/tap/vo"
        box_width = min(width - 4, 72)
        inner_width = max(16, box_width - 4)
        border_attr = self._color(1) | curses.A_BOLD
        heading_attr = self._color(1) | curses.A_BOLD
        muted_attr = curses.A_DIM
        # An underlined cyan URL signals that it is directly clickable.
        link_attr = self._color(1) | curses.A_BOLD | curses.A_UNDERLINE

        # Keep the command visually distinct from explanatory text.  This is
        # intentionally an ASCII-style code block rather than an instruction
        # lost in a paragraph of terminal text.
        command_width = min(inner_width, len(command) + 4)
        # Both sides of the command get exactly one space.  Keeping this row
        # the same width as its top and bottom borders prevents a dangling
        # right-hand gap in the code block.
        command_padding = command_width - len(command) - 3
        command_block = [
            ("┌" + "─" * (command_width - 2) + "┐", muted_attr),
            ("│ " + command + " " * command_padding + "│", muted_attr),
            ("└" + "─" * (command_width - 2) + "┘", muted_attr),
        ]

        if self.installer.running:
            lines: list[tuple[str, int]] = [
                ("Installing vo with Homebrew…", heading_attr),
                ("I will use command:", muted_attr),
                *command_block,
                ("", curses.A_NORMAL),
                (self.install_message or "Please wait…", muted_attr),
                ("", curses.A_NORMAL),
                ("Q / Ctrl+C cancels setup", muted_attr),
            ]
        else:
            lines = [
                ("Install manually", curses.A_NORMAL),
                ("https://github.com/k1LoW/vo", link_attr),
                ("", curses.A_NORMAL),
                ("Or press Space for automatic setup", curses.A_NORMAL),
                ("I will use command:", muted_attr),
                *command_block,
            ]
            if self.install_message:
                lines.extend([("", curses.A_NORMAL), (self.install_message, muted_attr)])

        # Leave two clear rows after the centered title so it reads as a
        # header rather than the first item in the setup instructions.
        display_lines: list[tuple[str, int, bool]] = [
            ("", curses.A_NORMAL, False),
            ("", curses.A_NORMAL, False),
        ]
        for line, attr in lines:
            is_link = line == "https://github.com/k1LoW/vo"
            display_lines.extend((part, attr, is_link) for part in (wrap(line, inner_width) or [""]))
        box_height = len(display_lines) + 3
        x = max(1, (width - box_width) // 2)
        y = max(2, (height - box_height) // 2)
        self._safe_add(y, x, "┌" + "─" * (box_width - 2) + "┐", border_attr)
        self._safe_add(y + 1, x, "│", border_attr)
        title = "vo setup needed"
        title_x = x + max(2, (box_width - len(title)) // 2)
        self._safe_add(y + 1, title_x, title, heading_attr)
        self._safe_add(y + 1, x + box_width - 1, "│", border_attr)
        for index, (line, attr, is_link) in enumerate(display_lines, 2):
            row = y + index
            self._safe_add(row, x, "│", border_attr)
            line_x = x + max(2, (box_width - len(line)) // 2)
            self._safe_add(row, line_x, line, attr)
            self._safe_add(row, x + box_width - 1, "│", border_attr)
            if is_link:
                # Mouse coordinates from SGR are one-based, while curses rows
                # and columns are zero-based.
                self._setup_link_hitbox = (line_x + 1, line_x + len(line), row + 1)
        bottom = y + box_height - 1
        self._safe_add(bottom, x, "└" + "─" * (box_width - 2) + "┘", border_attr)

    def _render_rows(
        self, split: int, width: int, max_rows: int
    ) -> list[tuple[Optional[str], str, str, int]]:
        all_rows = self._all_rows(split, width)
        if not all_rows:
            waiting = (
                "All set! Waiting for input from speakers..."
                if self.vo.running
                else "Waiting for start (press Space)"
            )
            return [(None, waiting, "", curses.A_DIM)]
        end = max(0, len(all_rows) - self.scroll)
        start = max(0, end - max_rows)
        return all_rows[start:end]

    def _all_rows(self, split: int, width: int) -> list[tuple[Optional[str], str, str, int]]:
        left_width = max(12, split - 2)
        source_width = max(8, left_width - 9)
        right_width = max(12, width - split - 5)
        all_rows: list[tuple[Optional[str], str, str, int]] = []
        for segment in self.segments:
            left_lines = wrap(segment.source, source_width) or [""]
            translated = segment.translation or "…"
            right_lines = wrap(translated, right_width) or [""]
            line_count = max(len(left_lines), len(right_lines))
            for i in range(line_count):
                all_rows.append((
                    segment.timestamp if i == 0 else "",
                    left_lines[i] if i < len(left_lines) else "",
                    right_lines[i] if i < len(right_lines) else "",
                    curses.A_NORMAL,
                ))
            all_rows.append((None, "", "", curses.A_NORMAL))
        return all_rows

    @staticmethod
    def _short_time(timestamp: str) -> str:
        try:
            return datetime.fromisoformat(timestamp).strftime("%H:%M:%S")
        except ValueError:
            return timestamp[-8:] if len(timestamp) >= 8 else timestamp

    def _safe_add(self, y: int, x: int, text: str, attr: int = 0) -> None:
        try:
            self.screen.addstr(y, x, text, attr)
        except curses.error:
            pass

    def _hline(self, y: int, width: int) -> None:
        try:
            self.screen.hline(y, 0, curses.ACS_HLINE, width)
        except curses.error:
            pass

    @staticmethod
    def _color(number: int) -> int:
        return curses.color_pair(number) if curses.has_colors() else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TUI wrapper for the on-device vo translator")
    parser.add_argument("--vo", default="vo", help="Path to the vo executable (default: vo)")
    parser.add_argument("--source", default="en-US", help="Source locale for vo (default: en-US)")
    parser.add_argument("--target", default="ru-RU", help="Target locale for vo (default: ru-RU)")
    parser.add_argument("--include-mic", action="store_true", help="Include microphone audio as well as system audio")
    parser.add_argument("--max-lines", type=int, default=500, help="Finalized segments held in memory (default: 500)")
    parser.add_argument(
        "--save-dir",
        type=Path,
        default=Path.cwd() / "saved_sessions",
        help="Folder used by the S save shortcut (default: ./saved_sessions)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.max_lines < 10:
        raise SystemExit("--max-lines must be at least 10")
    # Keep the TUI in the terminal's alternate screen buffer.  In particular,
    # this prevents Terminal.app's normal scrollback from moving the entire
    # interface when the user turns the mouse wheel.
    sys.stdout.write("\x1b[?1049h\x1b[H")
    sys.stdout.flush()
    try:
        curses.wrapper(lambda screen: Tui(screen, args).run())
    except KeyboardInterrupt:
        # Tui.run() has already stopped vo in its finally block.
        pass
    finally:
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
