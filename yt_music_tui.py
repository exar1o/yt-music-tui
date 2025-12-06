#!/usr/bin/env python3

import curses
import json
import os
import subprocess
import threading
import time
import shutil
import sys
import socket
import locale
from pathlib import Path
from typing import List, Dict, Optional, Set, Any
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor

locale.setlocale(locale.LC_ALL, '')

# ============================================================================
# CONFIGURATION
# ============================================================================

CONFIG = {
    "MPV_SOCKET": "/tmp/mpv_music_socket",
    "HISTORY_FILE": Path.home() / ".cache" / "yt_music_history.json",
    "MAX_DURATION": 360,
    "MIN_QUEUE": 5,
    "BAD_KEYWORDS": {
        "reaction", "react", "review", "gameplay", "walkthrough",
        "tutorial", "how to", "unboxing", "analysis", "explained",
        "lesson", "cover by", "remix contest", "teaser", "trailer",
        "full album", "album reaction", "commentary", "behind the scenes"
    },
    "GOOD_KEYWORDS": {
        "official", "audio", "lyrics", "video", "topic", "vevo", "hq", "visualizer"
    },
}

COLORS = {
    "header": 1,
    "status_ok": 2,
    "status_err": 3,
    "accent": 4,
    "nowplaying": 5,
    "queue": 6,
    "dim": 7,
    "highlight": 8,
    "divider": 9,
}

# Global flag to skip repeated cookie prompts in same session
_skip_cookie_check = False


# ============================================================================
# UTILITIES
# ============================================================================

class QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def truncate(text: str, width: int) -> str:
    if not text or width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[:width - 3] + "…"


def draw_box(stdscr, y: int, x: int, h: int, w: int, title: str = "", color: int = 0, rounded: bool = False):
    if w < 2 or h < 2:
        return
    try:
        if rounded:
            tl, tr, bl, br = "╭", "╮", "╰", "╯"
            h_char, v_char = "─", "│"
        else:
            tl, tr, bl, br = "┌", "┐", "└", "┘"
            h_char, v_char = "─", "│"

        if title and len(title) + 4 <= w:
            title_pad = (w - len(title) - 2) // 2
            top_line = tl + h_char * title_pad + " " + title + " " + h_char * (w - title_pad - len(title) - 3) + tr
        else:
            top_line = tl + h_char * (w - 2) + tr

        stdscr.addstr(y, x, top_line, color)
        for i in range(1, h - 1):
            stdscr.addstr(y + i, x, v_char, color)
            stdscr.addstr(y + i, x + w - 1, v_char, color)
        stdscr.addstr(y + h - 1, x, bl + h_char * (w - 2) + br, color)
    except curses.error:
        pass


def center_text(text: str, width: int) -> str:
    if len(text) >= width:
        return text
    padding = (width - len(text)) // 2
    return " " * padding + text + " " * (width - padding - len(text))


# ============================================================================
# COOKIE DETECTION
# ============================================================================

def check_ytdl_cookies() -> bool:
    """Test if yt-dlp can access a common music video without being blocked."""
    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL({"quiet": True, "logger": QuietLogger()}) as ydl:
            ydl.extract_info("https://www.youtube.com/watch?v=dQw4w9WgXcQ", download=False)
        return True
    except Exception:
        return False


def show_cookie_prompt(stdscr) -> bool:
    """Show cookie warning. Return True if user presses 'S' to skip future checks."""
    h, w = stdscr.getmaxyx()
    stdscr.clear()
    stdscr.addstr(2, 2, "⚠️  YouTube cookies not detected!", curses.color_pair(3) | curses.A_BOLD)
    stdscr.addstr(4, 2, "Some videos (especially age-restricted) may fail to play.", curses.A_DIM)
    stdscr.addstr(6, 2, "Options:", curses.A_BOLD)
    stdscr.addstr(8, 4, "- Press ANY KEY to continue without cookies")
    stdscr.addstr(9, 4, "- Press 'S' to skip this warning for the rest of this session")
    stdscr.addstr(11, 2, "Tip: Use 'cookies.txt' browser extension to export to ~/.yt-dlp-cookies.txt", curses.A_DIM)
    stdscr.refresh()

    while True:
        key = stdscr.getch()
        if key == ord('S') or key == ord('s'):
            return True
        elif key != -1:
            return False


# ============================================================================
# PROGRESS TRACKER
# ============================================================================

class ProgressTracker:
    def __init__(self):
        self.last_sync_time = 0.0
        self.known_pos = 0.0
        self.duration = 0.0
        self.is_paused = False
        self.playback_rate = 1.0
        self._lock = threading.Lock()

    def update(self, pos: float, dur: float, paused: bool):
        with self._lock:
            self.known_pos = pos
            self.duration = dur
            self.is_paused = paused
            self.last_sync_time = time.time()

    def get_estimated_pos(self) -> float:
        with self._lock:
            if self.is_paused or self.known_pos == 0:
                return self.known_pos
            elapsed = time.time() - self.last_sync_time
            estimated = self.known_pos + (elapsed * self.playback_rate)
            if self.duration > 0:
                return min(estimated, self.duration)
            return estimated

    def get_duration(self) -> float:
        with self._lock:
            return self.duration


# ============================================================================
# AUDIO ENGINE
# ============================================================================

class AudioEngine:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.proc: Optional[subprocess.Popen] = None
        self.volume = 100
        self._sock: Optional[socket.socket] = None
        self._req_id = 0
        self._is_starting = False
        self._track_start_time = 0.0
        self._skip_pending = False
        self.tracker = ProgressTracker()
        self._status_thread = None
        self._stop_status = False
        self._cleanup_socket()

    def _cleanup_socket(self):
        try:
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)
        except OSError:
            pass

    def _close_ipc(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _connect_ipc(self) -> bool:
        if self._sock is not None:
            return True
        if not os.path.exists(self.socket_path):
            return False
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(self.socket_path)
            self._sock = s
            return True
        except OSError:
            self._sock = None
            return False

    def play(self, url: str):
        if not self._validate_url(url):
            return False
        self.stop()
        self._is_starting = True
        self._track_start_time = time.time()
        self._skip_pending = False
        self.tracker = ProgressTracker()

        cmd = [
            "mpv", url,
            "--no-video",
            "--no-terminal",
            f"--input-ipc-server={self.socket_path}",
            f"--volume={self.volume}",
            "--ytdl-format=bestaudio",
        ]

        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        self._stop_status = False
        self._status_thread = threading.Thread(target=self._poll_status, daemon=True)
        self._status_thread.start()
        return True

    def _validate_url(self, url: str) -> bool:
        try:
            opts = {"quiet": True, "skip_download": True, "logger": QuietLogger(), "socket_timeout": 5}
            from yt_dlp import YoutubeDL
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info is not None and info.get("id") is not None
        except Exception:
            return False

    def _poll_status(self):
        while not self._stop_status and self.is_running():
            try:
                pos = self.get_property("time-pos")
                dur = self.get_property("duration")
                pause = self.get_property("pause")
                if pos is not None and dur is not None:
                    self.tracker.update(float(pos), float(dur), bool(pause))
                time.sleep(1.5)
            except Exception:
                time.sleep(1)

    def stop(self):
        self._stop_status = True
        if self.is_running():
            self._send_command(["quit"], expect_reply=False)
            try:
                self.proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self._close_ipc()
        self._cleanup_socket()
        self._is_starting = False
        self._skip_pending = False

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def is_starting(self) -> bool:
        if not self.is_running():
            return False
        return time.time() - self._track_start_time < 3.0

    def toggle_pause(self) -> bool:
        res = self._send_command(["cycle", "pause"], expect_reply=True)
        if res:
            threading.Thread(target=self._poll_status_once, daemon=True).start()
        return res is not None

    def _poll_status_once(self):
        time.sleep(0.1)
        try:
            pos = self.get_property("time-pos")
            dur = self.get_property("duration")
            pause = self.get_property("pause")
            if pos is not None:
                self.tracker.update(float(pos), float(dur), bool(pause))
        except:
            pass

    def change_volume(self, delta: int):
        self.volume = max(0, min(150, self.volume + delta))
        self._send_command(["set", "volume", str(self.volume)], expect_reply=False)

    def seek(self, seconds: int):
        if self.is_running():
            self._send_command(["seek", str(seconds), "relative"], expect_reply=False)
            threading.Thread(target=self._poll_status_once, daemon=True).start()

    def get_property(self, prop: str) -> Optional[Any]:
        try:
            response = self._send_command(["get_property", prop], expect_reply=True)
            if isinstance(response, dict):
                return response.get("data")
            return None
        except Exception:
            return None

    def _send_command(self, command: List, expect_reply: bool) -> Optional[Dict]:
        if not self.is_running():
            return None
        if not self._connect_ipc():
            return None

        self._req_id += 1
        payload = json.dumps({
            "command": command,
            "request_id": self._req_id if expect_reply else None
        }).encode() + b"\n"

        try:
            self._sock.sendall(payload)
        except (BrokenPipeError, OSError):
            self._close_ipc()
            return None

        if not expect_reply:
            return {}

        for attempt in range(2):
            try:
                data = b""
                end_time = time.time() + (0.5 * (attempt + 1))
                while b"\n" not in data and time.time() < end_time:
                    chunk = self._sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if not data:
                    continue
                line = data.split(b"\n", 1)[0]
                return json.loads(line.decode("utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError, ValueError):
                time.sleep(0.1 * (attempt + 1))
        return None


# ============================================================================
# MUSIC MANAGER
# ============================================================================

class MusicManager:
    def __init__(self, config: dict):
        self.config = config
        self.queue: List[Dict] = []
        self.queue_ids: Set[str] = set()
        self.played_ids: Set[str] = set()
        self.history: List[Dict] = []
        self.current_track: Optional[Dict] = None
        self.is_loading = False
        self.status_message = "Ready"
        self.last_play_time = 0.0
        self.shuffle_mode = False
        self.repeat_mode = "none"
        self._expand_lock = threading.Lock()
        self.url_cache: Dict[str, str] = {}
        self.cache_lock = threading.Lock()
        self._thread_pool = ThreadPoolExecutor(max_workers=2)
        self._load_history()

    def _load_history(self):
        hist_file = self.config["HISTORY_FILE"]
        if not hist_file.exists():
            return
        try:
            with open(hist_file) as f:
                data = json.load(f)
                self.history = data[-100:]
                self.played_ids = {item["id"] for item in self.history if "id" in item}
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    def _save_history(self, entry: Dict):
        self.history.append(entry)
        try:
            self.config["HISTORY_FILE"].parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.config["HISTORY_FILE"].with_suffix(".tmp")
            with open(tmp_file, "w") as f:
                json.dump(self.history[-100:], f)
            tmp_file.replace(self.config["HISTORY_FILE"])
        except OSError:
            pass

    def search(self, query: str, engine: AudioEngine):
        self.is_loading = True
        self.status_message = f"Searching: {query}..."
        threading.Thread(target=self._bg_search, args=(query, engine), daemon=True).start()

    def _bg_search(self, query: str, engine: AudioEngine):
        try:
            opts = {
                "quiet": True,
                "noplaylist": True,
                "skip_download": True,
                "extract_flat": "in_playlist",
                "logger": QuietLogger(),
                "socket_timeout": 10,
            }
            from yt_dlp import YoutubeDL
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=False)

            entries = info.get("entries", [])
            if not entries:
                self.status_message = "No results found."
                return

            track = self._pack_entry(entries[0])
            self._thread_pool.submit(self._resolve_url, track)
            self._play_track(track, engine)
            self.status_message = "Playing... (loading mix)"
            self.expand_queue_genre_mix(track)

        except Exception as e:
            self.status_message = f"Error: {str(e)[:40]}"
        finally:
            self.is_loading = False

    def _resolve_url(self, track: Dict):
        if track.get("id") in self.url_cache:
            return
        try:
            track_id = track.get("id")
            url = track.get("webpage_url")
            if track_id and url:
                with self.cache_lock:
                    self.url_cache[track_id] = url
        except Exception:
            pass

    def play_next(self, engine: AudioEngine):
        if engine._skip_pending or engine.is_starting():
            return

        with self._expand_lock:
            if engine._skip_pending or time.time() - self.last_play_time < 0.5:
                return
            engine._skip_pending = True

        try:
            if self.shuffle_mode and self.queue:
                import random
                random.shuffle(self.queue)

            while self.queue:
                candidate = self.queue.pop(0)
                cid = candidate.get("id")
                if cid:
                    self.queue_ids.discard(cid)
                if cid and cid not in self.played_ids:
                    self._play_track(candidate, engine)
                    if self.queue:
                        self._thread_pool.submit(self._resolve_url, self.queue[0])
                    if len(self.queue) < self.config["MIN_QUEUE"]:
                        self.auto_expand_queue()
                    return

            if self.repeat_mode == "one" and self.current_track:
                self._play_track(self.current_track, engine)
            elif self.current_track and not self.is_loading:
                self.status_message = "Autoplay: Extending mix..."
                self.expand_queue_genre_mix(self.current_track)
            elif self.repeat_mode == "all" and self.history:
                self.status_message = "Repeat all: Restarting..."
                self.queue = self.history[-25:]
                self.queue_ids = {t["id"] for t in self.queue}
                self.played_ids.clear()
            else:
                self.status_message = "Queue empty."
        finally:
            engine._skip_pending = False

    def _play_track(self, track: Dict, engine: AudioEngine):
        self.current_track = track
        if track.get("id"):
            self.played_ids.add(track["id"])
        self.last_play_time = time.time()
        self._save_history({
            "id": track.get("id", ""),
            "title": track.get("title", "Unknown"),
            "uploader": track.get("uploader", "Unknown"),
            "timestamp": time.time()
        })
        self.status_message = f"Playing: {track.get('title', 'Unknown')[:50]}"

        track_id = track.get("id")
        if track_id in self.url_cache:
            track["webpage_url"] = self.url_cache[track_id]

        engine.play(track["webpage_url"])

    def skip_to_track(self, index: int, engine: AudioEngine):
        if index < 1 or index > len(self.queue):
            self.status_message = f"Track #{index} not in queue (1–{len(self.queue)})."
            return

        track = self.queue[index - 1]
        for t in self.queue[:index - 1]:
            tid = t.get("id")
            if tid:
                self.queue_ids.discard(tid)
        self.queue = self.queue[index - 1:]
        self._play_track(track, engine)

    def auto_expand_queue(self):
        with self._expand_lock:
            if self.is_loading or len(self.queue) >= self.config["MIN_QUEUE"]:
                return
        base = self.queue[-1] if self.queue else self.current_track
        if base:
            self.expand_queue_genre_mix(base)

    def expand_queue_genre_mix(self, base_track: Dict):
        with self._expand_lock:
            if self.is_loading:
                return
            self.is_loading = True
        self._thread_pool.submit(self._bg_expand_yt_recommendations, base_track)

    def _bg_expand_yt_recommendations(self, base_track: Dict):
        try:
            vid_id = base_track.get("id")
            if not vid_id:
                return

            url = f"https://www.youtube.com/watch?v={vid_id}&list=RD{vid_id}"

            opts = {
                "quiet": True,
                "noplaylist": False,
                "skip_download": True,
                "extract_flat": "in_playlist",
                "logger": QuietLogger(),
                "playlist_items": "2-25",
                "socket_timeout": 10,
            }

            from yt_dlp import YoutubeDL
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)

            entries = info.get("entries", []) or []
            added = 0
            base_title = base_track.get("title", "")

            for e in entries:
                if not e or not self._is_valid_music(e):
                    continue

                vid_id_e = e.get("id")
                if not vid_id_e or vid_id_e in self.played_ids or vid_id_e in self.queue_ids:
                    continue
                if self._is_too_similar(base_title, e.get("title", "")):
                    continue

                packed = self._pack_entry(e)
                self.queue.append(packed)
                self.queue_ids.add(vid_id_e)
                added += 1
                self._thread_pool.submit(self._resolve_url, packed)

                if len(self.queue) >= self.config["MIN_QUEUE"] + 5:
                    break

            if added > 0:
                self.status_message = f"Queue ready ({len(self.queue)} songs)"

        except Exception as e:
            self.status_message = f"Mix failed: {str(e)[:30]}"
        finally:
            self.is_loading = False

    def _is_valid_music(self, entry: Dict) -> bool:
        title = str(entry.get("title", "")).lower()
        uploader = str(entry.get("uploader", "")).lower()
        duration = entry.get("duration")

        if duration is not None:
            try:
                if float(duration) > self.config["MAX_DURATION"]:
                    return False
            except (ValueError, TypeError):
                pass

        if any(kw in title for kw in self.config["BAD_KEYWORDS"]):
            return False
        if any(kw in title for kw in self.config["GOOD_KEYWORDS"]):
            return True
        if uploader and any(trusted in uploader for trusted in ["vevo", "topic", "official"]):
            return True
        return True

    def _is_too_similar(self, a: str, b: str) -> bool:
        if not a or not b:
            return False
        return SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.7

    def _pack_entry(self, e: Dict) -> Dict:
        vid = e.get("id", "")
        return {
            "id": vid,
            "title": e.get("title", "Unknown"),
            "uploader": e.get("uploader", "Unknown"),
            "webpage_url": f"https://www.youtube.com/watch?v={vid}",
            "tags": e.get("tags", []),
            "duration": e.get("duration"),
        }

    def toggle_shuffle(self):
        self.shuffle_mode = not self.shuffle_mode
        self.status_message = f"Shuffle: {'ON' if self.shuffle_mode else 'OFF'}"

    def toggle_repeat(self):
        modes = ["none", "one", "all"]
        idx = modes.index(self.repeat_mode)
        self.repeat_mode = modes[(idx + 1) % 3]
        self.status_message = f"Repeat: {self.repeat_mode.upper()}"


# ============================================================================
# TUI
# ============================================================================

def main_tui(stdscr):
    global _skip_cookie_check

    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()

    for i in range(1, 10):
        curses.init_pair(i, i, -1)

    stdscr.nodelay(True)

    # Cookie check on first run
    if not _skip_cookie_check and not check_ytdl_cookies():
        skip = show_cookie_prompt(stdscr)
        if skip:
            _skip_cookie_check = True

    engine = AudioEngine(CONFIG["MPV_SOCKET"])
    mgr = MusicManager(CONFIG)

    input_buffer = ""
    input_mode = False
    skip_mode = False

    try:
        while True:
            h, w = stdscr.getmaxyx()
            stdscr.erase()

            # HEADER
            try:
                header_text = "♪  yt-dlp Music  ♪"
                centered = center_text(header_text, w)
                stdscr.addstr(0, 0, centered[:w], curses.color_pair(COLORS["header"]) | curses.A_BOLD)
            except curses.error:
                pass

            y = 2

            # NOW PLAYING
            try:
                draw_box(stdscr, y, 0, 9, w, "NOW PLAYING", curses.color_pair(COLORS["nowplaying"]), rounded=True)

                if mgr.current_track:
                    title = mgr.current_track.get("title", "Unknown")
                    artist = mgr.current_track.get("uploader", "Unknown")

                    stdscr.addstr(y + 2, 3, "▸ ", curses.color_pair(COLORS["accent"]) | curses.A_BOLD)
                    stdscr.addstr(y + 2, 5, truncate(title, w - 8), curses.color_pair(COLORS["nowplaying"]) | curses.A_BOLD)
                    stdscr.addstr(y + 3, 3, "  ", curses.color_pair(COLORS["dim"]))
                    stdscr.addstr(y + 3, 5, truncate(artist, w - 8), curses.color_pair(COLORS["dim"]))

                    progress_pos = engine.tracker.get_estimated_pos()
                    progress_dur = engine.tracker.get_duration()

                    if progress_dur > 0:
                        ratio = max(0.0, min(1.0, progress_pos / progress_dur))
                        bar_w = min(w - 8, 60)
                        filled = int(bar_w * ratio)
                        bar = "█" * filled + "░" * (bar_w - filled)
                        time_str = f"{int(progress_pos//60)}:{int(progress_pos%60):02d} / {int(progress_dur//60)}:{int(progress_dur%60):02d}"
                    else:
                        bar_w = min(w - 8, 60)
                        bar = "░" * bar_w
                        time_str = "0:00 / ??"

                    stdscr.addstr(y + 5, 3, bar, curses.color_pair(COLORS["accent"]))
                    stdscr.addstr(y + 6, 3, time_str, curses.color_pair(COLORS["dim"]) | curses.A_DIM)
                else:
                    stdscr.addstr(y + 4, 3, "No track loaded", curses.color_pair(COLORS["dim"]) | curses.A_DIM)

            except curses.error:
                pass

            y += 10

            # STATUS
            try:
                status_text = mgr.status_message
                mode_indicators = []
                if mgr.shuffle_mode:
                    mode_indicators.append("SHUFFLE")
                if mgr.repeat_mode != "none":
                    mode_indicators.append(f"REPEAT {mgr.repeat_mode.upper()}")
                if mode_indicators:
                    status_text += f" [{'|'.join(mode_indicators)}]"

                status_color = COLORS["status_err"] if "Error" in status_text or "failed" in status_text.lower() else COLORS["status_ok"]

                if mgr.is_loading:
                    loading_anim = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"][int(time.time() * 10) % 10]
                    status_text = f"{loading_anim} {status_text}"

                stdscr.addstr(y, 0, truncate(status_text, w), curses.color_pair(status_color) | curses.A_BOLD)
            except curses.error:
                pass

            y += 2

            # QUEUE
            max_queue_lines = max(0, h - y - 3)
            queue_title = f"QUEUE ({len(mgr.queue)}/{CONFIG['MIN_QUEUE']})"
            queue_color = COLORS["status_ok"] if len(mgr.queue) >= CONFIG["MIN_QUEUE"] else COLORS["status_err"]

            try:
                draw_box(stdscr, y, 0, max_queue_lines + 2, w, queue_title, curses.color_pair(queue_color), rounded=True)

                for i, item in enumerate(mgr.queue[:max_queue_lines - 1]):
                    if i >= max_queue_lines - 2:
                        break
                    line_num = f"{i + 1:2d}."
                    track_title = truncate(item.get("title", "Unknown"), w - 10)

                    try:
                        stdscr.addstr(y + i + 2, 3, line_num, curses.color_pair(COLORS["accent"]))
                        stdscr.addstr(y + i + 2, 7, track_title, curses.color_pair(COLORS["dim"]) | curses.A_DIM)
                    except curses.error:
                        pass

                if not mgr.queue:
                    try:
                        stdscr.addstr(y + 2, 3, "Queue will auto-fill...", curses.color_pair(COLORS["dim"]) | curses.A_DIM)
                    except curses.error:
                        pass

            except curses.error:
                pass

            # FOOTER
            try:
                if input_mode:
                    prompt = f"Search: {input_buffer}_"
                    stdscr.addstr(h - 1, 0, truncate(prompt, w), curses.color_pair(COLORS["accent"]) | curses.A_BOLD)
                elif skip_mode:
                    prompt = f"Skip to #: {input_buffer}_"
                    stdscr.addstr(h - 1, 0, truncate(prompt, w), curses.color_pair(COLORS["accent"]) | curses.A_BOLD)
                else:
                    help_text = "[/]Search [s]Skip [SPACE]Pause [n]Next [J]V+ [K]V- [,]<-10s [.]>+10s [h]Shuffle [r]Repeat [c]Clear [q]Quit"
                    stdscr.addstr(h - 1, 0, truncate(help_text, w), curses.A_REVERSE)
            except curses.error:
                pass

            stdscr.noutrefresh()
            curses.doupdate()
            time.sleep(0.03)

            if not engine.is_starting() and not engine.is_running() and mgr.current_track and not mgr.is_loading:
                if time.time() - mgr.last_play_time > 3.0 and not engine._skip_pending:
                    mgr.play_next(engine)
                    time.sleep(0.05)

            if len(mgr.queue) < CONFIG["MIN_QUEUE"] and not mgr.is_loading:
                mgr.auto_expand_queue()

            key = stdscr.getch()
            if key == -1:
                continue

            if input_mode:
                if key == 10:
                    input_mode = False
                    if input_buffer.strip():
                        mgr.search(input_buffer.strip(), engine)
                    input_buffer = ""
                elif key == 27:
                    input_mode = False
                    input_buffer = ""
                elif key in (127, 8, curses.KEY_BACKSPACE):
                    input_buffer = input_buffer[:-1]
                elif 32 <= key <= 126:
                    input_buffer += chr(key)
                continue

            if skip_mode:
                if key == 10:
                    skip_mode = False
                    if input_buffer.strip().isdigit():
                        mgr.skip_to_track(int(input_buffer.strip()), engine)
                    else:
                        mgr.status_message = "Invalid track number."
                    input_buffer = ""
                elif key == 27:
                    skip_mode = False
                    input_buffer = ""
                elif key in (127, 8, curses.KEY_BACKSPACE):
                    input_buffer = input_buffer[:-1]
                elif ord('0') <= key <= ord('9'):
                    input_buffer += chr(key)
                continue

            if key == ord('/'):
                input_mode = True
                input_buffer = ""
            elif key == ord('s'):
                skip_mode = True
                input_buffer = ""
            elif key == ord(' '):
                engine.toggle_pause()
            elif key == ord('n'):
                mgr.play_next(engine)
            elif key == ord('J') or key == ord('j'):
                engine.change_volume(5)
            elif key == ord('K') or key == ord('k'):
                engine.change_volume(-5)
            elif key == ord(','):
                engine.seek(-10)
            elif key == ord('.'):
                engine.seek(10)
            elif key == ord('h'):
                mgr.toggle_shuffle()
            elif key == ord('r'):
                mgr.toggle_repeat()
            elif key == ord('c'):
                mgr.queue.clear()
                mgr.queue_ids.clear()
                mgr.status_message = "Queue cleared."
            elif key == ord('q'):
                break

    finally:
        engine.stop()
        mgr._thread_pool.shutdown(wait=False)


def main():
    if not shutil.which("mpv"):
        print("Error: 'mpv' is required but not installed.", file=sys.stderr)
        sys.exit(1)

    try:
        curses.wrapper(main_tui)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    # Fix the history path to avoid permission issues
    CONFIG["HISTORY_FILE"] = Path.home() / ".cache" / "yt_music_history.json"
    CONFIG["HISTORY_FILE"].parent.mkdir(parents=True, exist_ok=True)
    main()