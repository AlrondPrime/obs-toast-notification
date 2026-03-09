"""
OBS stateful toast with:
- single EventClient connection
- thread-safe UI handoff via queue
- manual Tk update loop (no mainloop)
- cleaner shutdown on SIGINT / IDE stop
- single-window stateful notifications:
    1) Recording has started
    2) Saving recording
    3) Recording saved

Expected files next to this script:
- .env
- obs_logo.png

.env format:
    HOST=127.0.0.1
    PORT=4455
    PASSWORD=your_password

Requires:
    pip install pillow python-dotenv obsws-python
"""

from __future__ import annotations

import ctypes
import os
import queue
import signal
import sys
import time
import tkinter as tk
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageTk
from obsws_python import EventClient


SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

HOST = os.getenv("HOST")
PORT = int(os.getenv("PORT"))
PASSWORD = os.getenv("PASSWORD")

WINDOW_W = 400
WINDOW_H = 120

TARGET_X = 2160
TARGET_Y = 140
HIDDEN_X = 2560

LEFT_STRIP_W = 6

LOGO_FILENAME = "obs_logo.png"
LOGO_X = 42
LOGO_Y = 23
LOGO_W = 66
LOGO_H = 66

TEXT_X = 144
TEXT_CENTER_Y = 60
FONT_FAMILY = "Verdana"
FONT_SIZE = 12

ANIM_IN_MS = 140
HOLD_MS = 1400
ANIM_OUT_MS = 100
FRAME_MS = 15
UI_POLL_SLEEP_S = 0.01

BG = "#232323"
GREEN = "#76B900"
TEXT = "#E7E7E7"

MSG_STARTED = "Recording has started"
MSG_SAVING = "Saving recording"
MSG_SAVED = "Recording saved"

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        print("[INFO] SetProcessDpiAwareness(2) applied")
        return
    except Exception as exc:
        print(f"[WARN] shcore DPI awareness failed: {exc}")

    try:
        ctypes.windll.user32.SetProcessDPIAware()
        print("[INFO] SetProcessDPIAware() applied")
    except Exception as exc:
        print(f"[WARN] user32 DPI awareness failed: {exc}")


class ToastController:
    def __init__(self) -> None:
        enable_dpi_awareness()

        self.m_logo_path = SCRIPT_DIR / LOGO_FILENAME
        if not self.m_logo_path.exists():
            raise SystemExit(f"Logo file not found: {self.m_logo_path}")

        self.m_root = tk.Tk()
        self.m_root.withdraw()
        self.m_root.overrideredirect(True)
        self.m_root.attributes("-topmost", True)
        self.m_root.configure(bg=BG)
        self.m_root.protocol("WM_DELETE_WINDOW", self.close)

        self.m_canvas = tk.Canvas(
            self.m_root,
            width=WINDOW_W,
            height=WINDOW_H,
            bg=BG,
            highlightthickness=0,
            bd=0,
        )
        self.m_canvas.pack()

        self.m_event_queue: "queue.SimpleQueue[str]" = queue.SimpleQueue()

        self.m_hide_after_id = None
        self.m_is_visible = False
        self.m_is_animating_in = False
        self.m_is_animating_out = False
        self.m_state_text = None
        self.m_anim_generation = 0
        self.m_hwnd = None
        self.m_is_closing = False

        self._build_ui()
        self._place(HIDDEN_X, TARGET_Y, alpha=1.0)

        self.m_root.deiconify()
        self.m_root.update_idletasks()
        self.m_hwnd = self.m_root.winfo_id()
        self._apply_window_styles()
        self._force_topmost()

    def _apply_window_styles(self) -> None:
        if sys.platform != "win32" or not self.m_hwnd:
            return

        try:
            exstyle = ctypes.windll.user32.GetWindowLongW(self.m_hwnd, GWL_EXSTYLE)
            exstyle |= WS_EX_TOOLWINDOW
            ctypes.windll.user32.SetWindowLongW(self.m_hwnd, GWL_EXSTYLE, exstyle)
        except Exception as exc:
            print(f"[WARN] Failed to apply WS_EX_TOOLWINDOW: {exc}")

    def _force_topmost(self) -> None:
        if self.m_is_closing:
            return

        self.m_root.lift()
        self.m_root.attributes("-topmost", True)

        if sys.platform != "win32" or not self.m_hwnd:
            return

        try:
            ctypes.windll.user32.SetWindowPos(
                self.m_hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        except Exception as exc:
            print(f"[WARN] SetWindowPos(HWND_TOPMOST) failed: {exc}")

    def _build_ui(self) -> None:
        self.m_canvas.create_rectangle(
            0, 0, WINDOW_W, WINDOW_H,
            fill=BG,
            outline="",
        )

        self.m_canvas.create_rectangle(
            0, 0, LEFT_STRIP_W, WINDOW_H,
            fill=GREEN,
            outline="",
        )

        logo = Image.open(self.m_logo_path).convert("RGBA")
        logo = logo.resize((LOGO_W, LOGO_H), Image.Resampling.LANCZOS)
        self.m_logo_photo = ImageTk.PhotoImage(logo)
        self.m_canvas.create_image(
            LOGO_X,
            LOGO_Y,
            image=self.m_logo_photo,
            anchor="nw",
        )

        self.m_text_id = self.m_canvas.create_text(
            TEXT_X,
            TEXT_CENTER_Y,
            text="",
            anchor="w",
            fill=TEXT,
            font=(FONT_FAMILY, FONT_SIZE),
        )

    def enqueue_state(self, text: str) -> None:
        if self.m_is_closing:
            return
        self.m_event_queue.put(text)

    def process_pending_events(self) -> None:
        if self.m_is_closing:
            return

        while True:
            try:
                text = self.m_event_queue.get_nowait()
            except queue.Empty:
                break
            self.show_state(text)

    def _set_text(self, text: str) -> None:
        self.m_canvas.itemconfigure(self.m_text_id, text=text)

    def _place(self, x: float, y: float, alpha: float = 1.0) -> None:
        if self.m_is_closing:
            return
        self.m_root.geometry(f"{WINDOW_W}x{WINDOW_H}+{int(x)}+{int(y)}")
        self.m_root.attributes("-alpha", max(0.0, min(1.0, alpha)))
        self.m_root.update_idletasks()
        self._force_topmost()

    @staticmethod
    def _ease_out_cubic(t: float) -> float:
        return 1.0 - (1.0 - t) ** 3

    @staticmethod
    def _ease_in_cubic(t: float) -> float:
        return t ** 3

    def _restart_hide_timer(self) -> None:
        if self.m_hide_after_id is not None:
            try:
                self.m_root.after_cancel(self.m_hide_after_id)
            except Exception:
                pass
            self.m_hide_after_id = None
        self.m_hide_after_id = self.m_root.after(HOLD_MS, self._hide)

    def _show_with_animation(self) -> None:
        self.m_anim_generation += 1
        my_generation = self.m_anim_generation
        self.m_is_animating_in = True
        self.m_is_animating_out = False

        start = time.perf_counter()
        duration_s = ANIM_IN_MS / 1000.0

        def step() -> None:
            if self.m_is_closing or my_generation != self.m_anim_generation:
                return

            elapsed = time.perf_counter() - start
            t = 1.0 if duration_s <= 0.0 else min(1.0, elapsed / duration_s)
            p = self._ease_out_cubic(t)
            x = HIDDEN_X + (TARGET_X - HIDDEN_X) * p
            self._place(x, TARGET_Y)

            if t < 1.0:
                self.m_root.after(FRAME_MS, step)
            else:
                if self.m_is_closing or my_generation != self.m_anim_generation:
                    return
                self.m_is_animating_in = False
                self.m_is_visible = True
                self._place(TARGET_X, TARGET_Y)
                self._restart_hide_timer()

        step()

    def _hide(self) -> None:
        if self.m_is_closing:
            return

        self.m_hide_after_id = None
        self.m_anim_generation += 1
        my_generation = self.m_anim_generation
        self.m_is_animating_out = True
        self.m_is_animating_in = False

        start = time.perf_counter()
        duration_s = ANIM_OUT_MS / 1000.0

        def step() -> None:
            if self.m_is_closing or my_generation != self.m_anim_generation:
                return

            elapsed = time.perf_counter() - start
            t = 1.0 if duration_s <= 0.0 else min(1.0, elapsed / duration_s)
            p = self._ease_in_cubic(t)
            x = TARGET_X + (HIDDEN_X - TARGET_X) * p
            self._place(x, TARGET_Y)

            if t < 1.0:
                self.m_root.after(FRAME_MS, step)
            else:
                if self.m_is_closing or my_generation != self.m_anim_generation:
                    return
                self.m_is_animating_out = False
                self.m_is_visible = False
                self._place(HIDDEN_X, TARGET_Y)

        step()

    def show_state(self, text: str) -> None:
        if self.m_is_closing:
            return

        print(f"[INFO] show_state -> {text}")

        if self.m_state_text == text and (self.m_is_visible or self.m_is_animating_in):
            self._restart_hide_timer()
            return

        self.m_state_text = text
        self._set_text(text)

        if self.m_is_visible or self.m_is_animating_in:
            self._restart_hide_timer()
            return

        if self.m_is_animating_out:
            self.m_anim_generation += 1
            self.m_is_animating_out = False
            self.m_is_visible = True
            self._place(TARGET_X, TARGET_Y)
            self._restart_hide_timer()
            return

        self._show_with_animation()

    def close(self) -> None:
        if self.m_is_closing:
            return

        print("[INFO] ToastController closing")
        self.m_is_closing = True
        self.m_anim_generation += 1

        if self.m_hide_after_id is not None:
            try:
                self.m_root.after_cancel(self.m_hide_after_id)
            except Exception:
                pass
            self.m_hide_after_id = None

        try:
            self.m_root.quit()
        except Exception:
            pass

        try:
            self.m_root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        try:
            while not self.m_is_closing:
                self.process_pending_events()
                self.m_root.update_idletasks()
                self.m_root.update()
                time.sleep(UI_POLL_SLEEP_S)
        except tk.TclError:
            pass
        finally:
            self.close()


class ObsBridge:
    def __init__(self, toast: ToastController) -> None:
        self.m_toast = toast
        self.m_events = EventClient(host=HOST, port=PORT, password=PASSWORD)
        self.m_events.callback.register(self.on_record_state_changed)
        self.m_last_output_state = None
        self.m_is_closed = False
        print(f"[INFO] Connected EventClient to OBS at {HOST}:{PORT}")

    def _dispatch_ui(self, text: str) -> None:
        if self.m_is_closed:
            return
        self.m_toast.enqueue_state(text)

    def on_record_state_changed(self, data) -> None:
        if self.m_is_closed:
            return

        output_state = getattr(data, "output_state", "")
        output_active = getattr(data, "output_active", None)
        print(f"[DEBUG] RecordStateChanged: output_state={output_state}, output_active={output_active}")

        if output_state == self.m_last_output_state:
            print("[DEBUG] Duplicate output_state ignored")
            return
        self.m_last_output_state = output_state

        if output_state in ("OBS_WEBSOCKET_OUTPUT_STARTED", "OBS_WEBSOCKET_OUTPUT_RESUMED"):
            self._dispatch_ui(MSG_STARTED)
            return

        if output_state in ("OBS_WEBSOCKET_OUTPUT_STOPPING",):
            self._dispatch_ui(MSG_SAVING)
            return

        if output_state in ("OBS_WEBSOCKET_OUTPUT_STOPPED",):
            self._dispatch_ui(MSG_SAVED)
            return

        print("[DEBUG] Transitional or unsupported record state ignored")

    def close(self) -> None:
        if self.m_is_closed:
            return
        self.m_is_closed = True
        print("[INFO] ObsBridge closing")
        try:
            self.m_events.disconnect()
            print("[INFO] EventClient disconnected")
        except Exception as exc:
            print(f"[WARN] EventClient disconnect failed: {exc}")


def main() -> None:
    toast = ToastController()
    bridge = None
    is_shutting_down = False

    def shutdown(*_args) -> None:
        nonlocal is_shutting_down
        if is_shutting_down:
            return
        is_shutting_down = True
        print("[INFO] Shutdown requested")
        try:
            if bridge is not None:
                bridge.close()
        finally:
            toast.close()

    try:
        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
    except Exception as exc:
        print(f"[WARN] Signal handlers not installed: {exc}")

    try:
        bridge = ObsBridge(toast)
    except Exception as exc:
        print(f"[ERROR] OBS connection failed: {exc}")
        print("[WARN] Over true exclusive fullscreen games regular desktop windows may stay behind the game.")
        print("[WARN] For reliable visibility use borderless/windowed mode, or switch to an injected/overlay approach.")

    try:
        toast.run()
    except KeyboardInterrupt:
        print("[INFO] KeyboardInterrupt received")
        shutdown()
    finally:
        shutdown()


if __name__ == "__main__":
    main()
