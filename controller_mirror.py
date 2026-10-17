#!/usr/bin/env python3
"""
Controller Mirror - let several game windows read the real controller at once.

The controller is already visible to every program, but most games ignore it while
they're in the background. This tool keeps telling every selected window that it
is the active window, so each instance keeps reading the real controller (analog
sticks and all) at the same moment.

Activation messages are re-sent the instant the real foreground window changes and
again on a short interval, in case a game gets deactivated some other way.

Windows only. Standard library only (ctypes + tkinter).
"""

import ctypes
import os
import sys
import threading
import time

if sys.platform != "win32":
    sys.exit("Controller Mirror only runs on Windows.")

import ctypes.wintypes as wt
import tkinter as tk
from tkinter import ttk

APP_NAME = "Controller Mirror"

# --------------------------------------------------------------------------- Win32

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
    dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
except OSError:
    dwmapi = None

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
WINEVENTPROC = ctypes.WINFUNCTYPE(
    None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG, wt.LONG, wt.DWORD, wt.DWORD)

user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetForegroundWindow.restype = wt.HWND
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.SetWinEventHook.argtypes = [
    wt.DWORD, wt.DWORD, wt.HMODULE, WINEVENTPROC, wt.DWORD, wt.DWORD, wt.DWORD]
user32.SetWinEventHook.restype = wt.HANDLE
user32.UnhookWinEvent.argtypes = [wt.HANDLE]
user32.MsgWaitForMultipleObjects.argtypes = [
    wt.DWORD, ctypes.c_void_p, wt.BOOL, wt.DWORD, wt.DWORD]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]

WM_ACTIVATE = 0x0006
WM_SETFOCUS = 0x0007
WM_KILLFOCUS = 0x0008
WM_ACTIVATEAPP = 0x001C
WA_INACTIVE = 0
WA_ACTIVE = 1
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
QS_ALLINPUT = 0x04FF
PM_REMOVE = 0x0001
ERROR_ACCESS_DENIED = 5

# Re-sent on every interval: "your app is active and this is the active window".
ACTIVATE_MSGS = ((WM_ACTIVATEAPP, 1, 0), (WM_ACTIVATE, WA_ACTIVE, 0))
# Sent when a window is added or the real foreground changes: also hand it focus.
FULL_ACTIVATE_MSGS = ACTIVATE_MSGS + ((WM_SETFOCUS, 0, 0),)
# Sent when a window is removed or mirroring stops, so the game knows the truth again.
DEACTIVATE_MSGS = ((WM_KILLFOCUS, 0, 0), (WM_ACTIVATE, WA_INACTIVE, 0), (WM_ACTIVATEAPP, 0, 0))


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


# --------------------------------------------------------------------- FocusKeeper

class FocusKeeper(threading.Thread):
    """Keeps every target window convinced that it is the active window."""

    def __init__(self):
        super().__init__(daemon=True, name="focus-keeper")
        # Written by the UI thread, read by this one (plain attribute swaps).
        self.targets = ()
        self.enabled = False
        self.interval = 0.1

        self._active = ()            # windows we are currently keeping active
        self._foreground_changed = False
        self._failures = {}          # hwnd -> "closed" | "blocked"
        self._failures_lock = threading.Lock()
        self._stop_event = threading.Event()
        # Keep a reference so the callback isn't garbage collected while hooked.
        self._hook_proc = WINEVENTPROC(self._on_foreground)

    def stop(self):
        self._stop_event.set()

    def pop_failures(self):
        with self._failures_lock:
            failures, self._failures = self._failures, {}
        return failures

    def run(self):
        # The hook is delivered through this thread's message queue, so it must be
        # installed here and this thread must keep pumping messages.
        hook = user32.SetWinEventHook(
            EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._hook_proc,
            0, 0, WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        msg = wt.MSG()
        next_tick = 0.0
        try:
            while not self._stop_event.is_set():
                wait_ms = int(max(0.0, min(next_tick - time.monotonic(), 0.1)) * 1000)
                user32.MsgWaitForMultipleObjects(0, None, False, wait_ms, QS_ALLINPUT)
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))

                self._sync_targets(self.targets if self.enabled else ())
                if self._foreground_changed:
                    # Windows just deactivated a game; re-activate everything at once.
                    self._foreground_changed = False
                    self._post(self._background(), FULL_ACTIVATE_MSGS)
                    next_tick = time.monotonic() + self.interval
                elif time.monotonic() >= next_tick:
                    self._post(self._background(), ACTIVATE_MSGS)
                    next_tick = time.monotonic() + self.interval
        finally:
            if hook:
                user32.UnhookWinEvent(hook)
            self._sync_targets(())

    def _on_foreground(self, hook, event, hwnd, id_object, id_child, thread, time_ms):
        self._foreground_changed = True

    def _background(self):
        """Active targets except the real foreground window, which needs no help."""
        foreground = user32.GetForegroundWindow()
        return tuple(h for h in self._active if h != foreground)

    def _sync_targets(self, targets):
        if targets == self._active:
            return
        removed = [h for h in self._active if h not in targets]
        added = [h for h in targets if h not in self._active]
        self._active = targets
        foreground = user32.GetForegroundWindow()
        self._post([h for h in removed if h != foreground], DEACTIVATE_MSGS)
        self._post([h for h in added if h != foreground], FULL_ACTIVATE_MSGS)

    def _post(self, hwnds, msgs):
        # Each message goes to every window before the next one, so all windows
        # are re-activated within microseconds of each other.
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
        self.keeper = FocusKeeper()

        root.title(APP_NAME)
        root.geometry("620x420")
        root.minsize(460, 300)
        self._build()
        self.keeper.start()
        self.refresh_windows()
        self._tick()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build(self):
        pad = {"padx": 8, "pady": 4}

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
        self.toggle_btn = ttk.Button(bottom, text="Start", command=self.toggle_running)
        self.toggle_btn.pack(side="left")
        ttk.Label(bottom, text="Re-activate every").pack(side="left", padx=(12, 4))
        self.interval_var = tk.StringVar(value="100")
        ttk.Spinbox(bottom, from_=10, to=1000, increment=10, width=6,
                    textvariable=self.interval_var).pack(side="left")
        self.interval_var.trace_add("write", lambda *_: self._on_interval())
        ttk.Label(bottom, text="ms").pack(side="left", padx=(4, 0))
        self.topmost_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="Always on top", variable=self.topmost_var,
                        command=lambda: self.root.attributes("-topmost", self.topmost_var.get())
                        ).pack(side="right")

        self.status = ttk.Label(self.root, text="", anchor="w", foreground="#555")
        self.status.pack(fill="x", padx=8, pady=(0, 6))
        self._set_status("Tick the game windows, then press Start.")

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
        self.keeper.targets = tuple(sorted(self.selected))
        self._update_running_status()

    # ---- controls

    def toggle_running(self):
        self.keeper.enabled = not self.keeper.enabled
        self.toggle_btn.configure(text="Stop" if self.keeper.enabled else "Start")
        self._update_running_status()

    def _on_interval(self):
        try:
            ms = int(self.interval_var.get())
        except ValueError:
            return
        self.keeper.interval = min(max(ms, 10), 1000) / 1000

    # ---- status

    def _set_status(self, text):
        self.status.configure(text=text)

    def _update_running_status(self):
        n = len(self.selected)
        windows = f"{n} window{'s' if n != 1 else ''}"
        if self.keeper.enabled:
            self._set_status(f"Keeping {windows} active.")
        else:
            self._set_status(f"{windows} selected. Stopped.")

    def _tick(self):
        failures = self.keeper.pop_failures()
        if any(why == "closed" for why in failures.values()):
            self.refresh_windows()
        if any(why == "blocked" for why in failures.values()):
            self._set_status("A window rejected the messages. The game is probably running as "
                             "administrator, so run Controller Mirror as administrator too.")
        self.root.after(200, self._tick)

    def on_close(self):
        self.keeper.stop()
        self.keeper.join(timeout=1)
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
