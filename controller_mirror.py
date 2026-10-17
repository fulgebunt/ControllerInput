#!/usr/bin/env python3
"""
Controller Mirror - drive several game windows from one controller at once.

Reads an Xbox/XInput controller and posts the mapped keystrokes straight into the
message queue of every selected window, so the windows keep receiving input while
they are in the background. Every state change is sent to all windows back-to-back
in one batch (a few microseconds apart).

Windows only. Standard library only (ctypes + tkinter). Key bindings live in
mapping.json next to this script; it is created with defaults on first run.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from collections import namedtuple

if sys.platform != "win32":
    sys.exit("Controller Mirror only runs on Windows.")

import ctypes.wintypes as wt
import tkinter as tk
from tkinter import messagebox, ttk

APP_NAME = "Controller Mirror"
HERE = os.path.dirname(os.path.abspath(__file__))
MAPPING_PATH = os.path.join(HERE, "mapping.json")

# --------------------------------------------------------------------------- Win32

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")
try:
    dwmapi = ctypes.WinDLL("dwmapi")
    dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
except OSError:
    dwmapi = None

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
user32.MapVirtualKeyW.restype = wt.UINT
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetCurrentThread.restype = wt.HANDLE
kernel32.SetThreadPriority.argtypes = [wt.HANDLE, ctypes.c_int]

WM_ACTIVATE = 0x0006
WM_SETFOCUS = 0x0007
WM_ACTIVATEAPP = 0x001C
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WA_ACTIVE = 1
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
THREAD_PRIORITY_HIGHEST = 2
ERROR_ACCESS_DENIED = 5

# Messages that make many games believe they have focus.
FOCUS_MSGS = (
    (WM_ACTIVATEAPP, 1, 0),
    (WM_ACTIVATE, WA_ACTIVE, 0),
    (WM_SETFOCUS, 0, 0),
)


def _signed_lparam(value):
    if ctypes.sizeof(ctypes.c_void_p) == 4 and value >= 1 << 31:
        value -= 1 << 32
    return value


def window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_name(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "?"
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return "?"
    finally:
        kernel32.CloseHandle(handle)


def is_cloaked(hwnd):
    if dwmapi is None:
        return False
    cloaked = wt.DWORD()
    res = dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return res == 0 and cloaked.value != 0


def list_windows():
    """Return {hwnd: (title, process_name)} for visible top-level app windows."""
    own_pid = os.getpid()
    found = {}

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        title = window_title(hwnd)
        if not title or is_cloaked(hwnd):
            return True
        pid = window_pid(hwnd)
        if pid != own_pid:
            found[hwnd] = (title, process_name(pid))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


# -------------------------------------------------------------------------- XInput

class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", wt.WORD),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wt.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


def _load_xinput():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            dll = ctypes.WinDLL(name)
        except OSError:
            continue
        dll.XInputGetState.argtypes = [wt.DWORD, ctypes.POINTER(XINPUT_STATE)]
        dll.XInputGetState.restype = wt.DWORD
        return dll
    sys.exit("XInput is not available on this system.")


xinput = _load_xinput()

BUTTON_BITS = {
    "DPAD_UP": 0x0001, "DPAD_DOWN": 0x0002, "DPAD_LEFT": 0x0004, "DPAD_RIGHT": 0x0008,
    "START": 0x0010, "BACK": 0x0020, "LS": 0x0040, "RS": 0x0080,
    "LB": 0x0100, "RB": 0x0200, "A": 0x1000, "B": 0x2000, "X": 0x4000, "Y": 0x8000,
}
ANALOG_INPUTS = (
    "LT", "RT",
    "LS_UP", "LS_DOWN", "LS_LEFT", "LS_RIGHT",
    "RS_UP", "RS_DOWN", "RS_LEFT", "RS_RIGHT",
)
INPUT_NAMES = tuple(BUTTON_BITS) + ANALOG_INPUTS


def active_inputs(pad, cfg):
    """Names of every controller input currently held down."""
    held = {name for name, bit in BUTTON_BITS.items() if pad.wButtons & bit}
    if pad.bLeftTrigger >= cfg.trigger_threshold:
        held.add("LT")
    if pad.bRightTrigger >= cfg.trigger_threshold:
        held.add("RT")
    dz = cfg.stick_deadzone
    for stick, x, y in (("LS", pad.sThumbLX, pad.sThumbLY), ("RS", pad.sThumbRX, pad.sThumbRY)):
        if x > dz:
            held.add(stick + "_RIGHT")
        elif x < -dz:
            held.add(stick + "_LEFT")
        if y > dz:
            held.add(stick + "_UP")
        elif y < -dz:
            held.add(stick + "_DOWN")
    return held


# ------------------------------------------------------------------------ Key names

# A key is the pair of fully-built messages that press and release it.
Key = namedtuple("Key", "down up")

_NAMED_VKS = {
    "BACKSPACE": 0x08, "TAB": 0x09, "ENTER": 0x0D, "RETURN": 0x0D, "PAUSE": 0x13,
    "CAPSLOCK": 0x14, "ESC": 0x1B, "ESCAPE": 0x1B, "SPACE": 0x20,
    "PAGEUP": 0x21, "PAGEDOWN": 0x22, "END": 0x23, "HOME": 0x24,
    "LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28,
    "INSERT": 0x2D, "DELETE": 0x2E,
    "MULTIPLY": 0x6A, "ADD": 0x6B, "SUBTRACT": 0x6D, "DECIMAL": 0x6E, "DIVIDE": 0x6F,
    "NUMLOCK": 0x90, "SCROLLLOCK": 0x91,
    "SEMICOLON": 0xBA, "EQUALS": 0xBB, "COMMA": 0xBC, "MINUS": 0xBD, "PERIOD": 0xBE,
    "SLASH": 0xBF, "GRAVE": 0xC0, "LBRACKET": 0xDB, "BACKSLASH": 0xDC,
    "RBRACKET": 0xDD, "QUOTE": 0xDE,
}
# Modifiers: window messages carry the generic VK; the scan code tells left from right.
_MODIFIERS = {
    "SHIFT": (0x10, 0x2A, False), "LSHIFT": (0x10, 0x2A, False), "RSHIFT": (0x10, 0x36, False),
    "CTRL": (0x11, 0x1D, False), "LCTRL": (0x11, 0x1D, False), "RCTRL": (0x11, 0x1D, True),
    "ALT": (0x12, 0x38, False), "LALT": (0x12, 0x38, False), "RALT": (0x12, 0x38, True),
}
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x6F, 0x90}


def _virtual_key(name):
    if name in _NAMED_VKS:
        return _NAMED_VKS[name]
    if len(name) == 1 and (name.isdigit() or "A" <= name <= "Z"):
        return ord(name)
    if name[0] == "F" and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return 0x70 + int(name[1:]) - 1
    if name.startswith("NUMPAD") and name[6:].isdigit() and len(name) == 7:
        return 0x60 + int(name[6:])
    if name.startswith("0X"):
        return int(name, 16)
    raise ValueError(f"unknown key name {name!r}")


def parse_key(name):
    name = name.strip().upper()
    if name in _MODIFIERS:
        vk, scan, extended = _MODIFIERS[name]
    else:
        vk = _virtual_key(name)
        scan = user32.MapVirtualKeyW(vk, 0)
        extended = vk in _EXTENDED_VKS
    is_alt = vk == 0x12
    lparam = 1 | (scan << 16) | (int(extended) << 24) | (int(is_alt) << 29)
    down_msg = WM_SYSKEYDOWN if is_alt else WM_KEYDOWN
    up_msg = WM_SYSKEYUP if is_alt else WM_KEYUP
    return Key(
        down=(down_msg, vk, _signed_lparam(lparam)),
        up=(up_msg, vk, _signed_lparam(lparam | (1 << 30) | (1 << 31))),
    )


# -------------------------------------------------------------------------- Config

DEFAULT_CONFIG = {
    "stick_deadzone": 0.35,
    "trigger_threshold": 0.25,
    "poll_interval_ms": 1,
    "bindings": {
        "A": "SPACE", "B": "Q", "X": "E", "Y": "R",
        "LB": "1", "RB": "2", "LT": "F", "RT": "G",
        "START": "ESCAPE", "BACK": "TAB", "LS": "LSHIFT", "RS": "C",
        "DPAD_UP": "UP", "DPAD_DOWN": "DOWN", "DPAD_LEFT": "LEFT", "DPAD_RIGHT": "RIGHT",
        "LS_UP": "W", "LS_DOWN": "S", "LS_LEFT": "A", "LS_RIGHT": "D",
        "RS_UP": "I", "RS_DOWN": "K", "RS_LEFT": "J", "RS_RIGHT": "L",
    },
}


class Config:
    def __init__(self, raw):
        self.stick_deadzone = min(max(float(raw["stick_deadzone"]), 0.0), 1.0) * 32767
        self.trigger_threshold = max(min(max(float(raw["trigger_threshold"]), 0.0), 1.0) * 255, 1)
        self.poll_interval = max(float(raw["poll_interval_ms"]), 0.0) / 1000
        self.bindings = {}
        problems = []
        for inp, keys in raw["bindings"].items():
            if inp.upper() not in INPUT_NAMES:
                problems.append(f"unknown controller input {inp!r}")
                continue
            if keys is None:
                continue
            if isinstance(keys, str):
                keys = [keys]
            try:
                self.bindings[inp.upper()] = tuple(parse_key(k) for k in keys)
            except (ValueError, AttributeError) as exc:
                problems.append(f"{inp}: {exc}")
        if problems:
            raise ValueError("\n".join(problems))

    def keys_for(self, inputs):
        return frozenset(k for name in inputs for k in self.bindings.get(name, ()))


def load_config():
    if not os.path.exists(MAPPING_PATH):
        with open(MAPPING_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    with open(MAPPING_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    return Config({**DEFAULT_CONFIG, **raw})


# -------------------------------------------------------------------------- Mirror

class Mirror(threading.Thread):
    """Polls the controller and posts key changes to every target window."""

    def __init__(self, config):
        super().__init__(daemon=True, name="mirror")
        # Written by the UI thread, read by this one (plain attribute swaps).
        self.config = config
        self.targets = ()
        self.user_index = 0
        self.enabled = False
        self.spoof_focus = False
        self.connected = False

        self._active = ()            # windows that currently hold our key state
        self._held = frozenset()     # keys currently pressed in those windows
        self._refocus = False
        self._failures = {}          # hwnd -> "closed" | "blocked"
        self._failures_lock = threading.Lock()
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def refocus(self):
        self._refocus = True

    def pop_failures(self):
        with self._failures_lock:
            failures, self._failures = self._failures, {}
        return failures

    def run(self):
        winmm.timeBeginPeriod(1)
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST)
        state = XINPUT_STATE()
        last_seen = None
        try:
            while not self._stop_event.is_set():
                cfg = self.config
                self._sync_targets(self.targets if self.enabled else ())

                index = self.user_index
                if xinput.XInputGetState(index, ctypes.byref(state)) != 0:
                    # Polling an empty slot is slow, so back off while disconnected.
                    self.connected = False
                    self._set_keys(frozenset())
                    last_seen = None
                    self._stop_event.wait(0.5)
                    continue
                self.connected = True

                seen = (index, state.dwPacketNumber, cfg)
                if seen != last_seen:
                    last_seen = seen
                    self._set_keys(cfg.keys_for(active_inputs(state.Gamepad, cfg)))
                time.sleep(cfg.poll_interval)
        finally:
            self._sync_targets(())
            winmm.timeEndPeriod(1)

    def _sync_targets(self, targets):
        if self._refocus:
            self._refocus = False
            if self.spoof_focus:
                self._post(self._active, FOCUS_MSGS)
        if targets == self._active:
            return
        removed = [h for h in self._active if h not in targets]
        added = [h for h in targets if h not in self._active]
        self._active = targets
        if removed and self._held:
            self._post(removed, [k.up for k in self._held])
        if added:
            if self.spoof_focus:
                self._post(added, FOCUS_MSGS)
            if self._held:
                self._post(added, [k.down for k in self._held])

    def _set_keys(self, keys):
        if keys == self._held:
            return
        msgs = [k.up for k in self._held - keys] + [k.down for k in keys - self._held]
        self._held = keys
        self._post(self._active, msgs)

    def _post(self, hwnds, msgs):
        # Each message goes to every window before the next message is sent, so
        # all windows see every press/release within microseconds of each other.
        post = user32.PostMessageW
        failed = []
        for msg, wparam, lparam in msgs:
            for hwnd in hwnds:
                if not post(hwnd, msg, wparam, lparam):
                    failed.append((hwnd, ctypes.get_last_error()))
        if not failed:
            return
        with self._failures_lock:
            for hwnd, err in failed:
                if not user32.IsWindow(hwnd):
                    self._failures[hwnd] = "closed"
                elif err == ERROR_ACCESS_DENIED:
                    self._failures[hwnd] = "blocked"
        dead = {h for h, _ in failed if not user32.IsWindow(h)}
        if dead:
            self._active = tuple(h for h in self._active if h not in dead)


# ------------------------------------------------------------------------------ UI

CHECKED, UNCHECKED = "☑", "☐"


class App:
    def __init__(self, root):
        self.root = root
        self.windows = {}       # hwnd -> (title, process)
        self.selected = set()

        try:
            config = load_config()
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            messagebox.showerror(APP_NAME, f"mapping.json has problems, using defaults:\n\n{exc}")
            config = Config(DEFAULT_CONFIG)
        self.mirror = Mirror(config)

        root.title(APP_NAME)
        root.geometry("620x440")
        root.minsize(460, 300)
        self._build()
        self.mirror.start()
        self.refresh_windows()
        self._tick()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build(self):
        pad = {"padx": 8, "pady": 4}

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Controller:").pack(side="left")
        self.index_var = tk.StringVar(value="1")
        index_box = ttk.Combobox(top, textvariable=self.index_var, values=["1", "2", "3", "4"],
                                 width=3, state="readonly")
        index_box.pack(side="left", padx=(4, 8))
        index_box.bind("<<ComboboxSelected>>",
                       lambda _: setattr(self.mirror, "user_index", int(self.index_var.get()) - 1))
        self.ctrl_label = ttk.Label(top, text="")
        self.ctrl_label.pack(side="left")
        self.topmost_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Always on top", variable=self.topmost_var,
                        command=lambda: self.root.attributes("-topmost", self.topmost_var.get())
                        ).pack(side="right")

        filt = ttk.Frame(self.root)
        filt.pack(fill="x", **pad)
        ttk.Label(filt, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.render_windows())
        ttk.Entry(filt, textvariable=self.filter_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(filt, text="Refresh", command=self.refresh_windows).pack(side="left")

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, **pad)
        self.tree = ttk.Treeview(body, columns=("sel", "title", "process"), show="headings",
                                 selectmode="browse")
        self.tree.heading("sel", text="")
        self.tree.heading("title", text="Window")
        self.tree.heading("process", text="Process")
        self.tree.column("sel", width=32, stretch=False, anchor="center")
        self.tree.column("title", width=380)
        self.tree.column("process", width=150, stretch=False)
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.bind("<ButtonRelease-1>", self._on_click)
        self.tree.bind("<space>", lambda _: self._toggle(self.tree.focus()))

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", **pad)
        self.toggle_btn = ttk.Button(bottom, text="Start mirroring", command=self.toggle_mirroring)
        self.toggle_btn.pack(side="left")
        self.spoof_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="Spoof focus", variable=self.spoof_var,
                        command=self._on_spoof).pack(side="left", padx=8)
        ttk.Button(bottom, text="Reload mapping", command=self.reload_mapping).pack(side="right")
        ttk.Button(bottom, text="Edit mapping", command=self.edit_mapping).pack(side="right", padx=4)

        self.status = ttk.Label(self.root, text="", anchor="w", foreground="#555")
        self.status.pack(fill="x", padx=8, pady=(0, 6))
        self._set_status("Tick the game windows to control, then press Start.")

    # ---- window list

    def refresh_windows(self):
        self.windows = list_windows()
        gone = self.selected - set(self.windows)
        if gone:
            self.selected -= gone
            self._push_targets()
        self.render_windows()

    def render_windows(self):
        needle = self.filter_var.get().lower()
        self.tree.delete(*self.tree.get_children())
        rows = sorted(self.windows.items(), key=lambda kv: (kv[1][1].lower(), kv[1][0].lower()))
        for hwnd, (title, proc) in rows:
            selected = hwnd in self.selected
            if selected or not needle or needle in title.lower() or needle in proc.lower():
                self.tree.insert("", "end", iid=str(hwnd),
                                 values=(CHECKED if selected else UNCHECKED, title, proc))

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell":
            self._toggle(self.tree.identify_row(event.y))

    def _toggle(self, iid):
        if not iid:
            return
        hwnd = int(iid)
        if hwnd in self.selected:
            self.selected.discard(hwnd)
        else:
            self.selected.add(hwnd)
        self.tree.set(iid, "sel", CHECKED if hwnd in self.selected else UNCHECKED)
        self._push_targets()

    def _push_targets(self):
        self.mirror.targets = tuple(sorted(self.selected))
        self._update_running_status()

    # ---- controls

    def toggle_mirroring(self):
        self.mirror.enabled = not self.mirror.enabled
        self.toggle_btn.configure(text="Stop mirroring" if self.mirror.enabled else "Start mirroring")
        self._update_running_status()

    def _on_spoof(self):
        self.mirror.spoof_focus = self.spoof_var.get()
        self.mirror.refocus()

    def edit_mapping(self):
        if not os.path.exists(MAPPING_PATH):
            with open(MAPPING_PATH, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
        subprocess.Popen(["notepad.exe", MAPPING_PATH])

    def reload_mapping(self):
        try:
            self.mirror.config = load_config()
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            messagebox.showerror(APP_NAME, f"Could not load mapping.json:\n\n{exc}")
            return
        self._set_status("Mapping reloaded.")

    # ---- status

    def _set_status(self, text):
        self.status.configure(text=text)

    def _update_running_status(self):
        n = len(self.selected)
        if self.mirror.enabled:
            self._set_status(f"Mirroring to {n} window{'s' if n != 1 else ''}.")
        else:
            self._set_status(f"{n} window{'s' if n != 1 else ''} selected. Mirroring is stopped.")

    def _tick(self):
        if self.mirror.connected:
            self.ctrl_label.configure(text="connected", foreground="#1a7f37")
        else:
            self.ctrl_label.configure(text="not connected", foreground="#b42318")

        failures = self.mirror.pop_failures()
        closed = {h for h, why in failures.items() if why == "closed"}
        if closed:
            self.refresh_windows()
        if any(why == "blocked" for why in failures.values()):
            self._set_status("A window rejected input. The game is probably running as "
                             "administrator, so run Controller Mirror as administrator too.")
        self.root.after(200, self._tick)

    def on_close(self):
        self.mirror.stop()
        self.mirror.join(timeout=1)
        self.root.destroy()


def main():
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(1)
    except (OSError, AttributeError):
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
