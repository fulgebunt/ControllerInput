# Controller Mirror

Windows-only, single-file Python tool (`controller_mirror.py`, stdlib only: ctypes + tkinter)
that keeps selected background windows convinced they are active (posted WM_ACTIVATEAPP /
WM_ACTIVATE / WM_SETFOCUS), so every game instance reads the real controller at once.
See README.md for usage.

## Structure
- Win32 ctypes bindings → `FocusKeeper` thread (installs an EVENT_SYSTEM_FOREGROUND WinEvent
  hook, pumps its own message loop, re-activates targets on foreground change and on an
  interval) → `App` tkinter UI.
- UI thread talks to `FocusKeeper` only via plain attribute swaps (`targets`, `enabled`,
  `interval`) and `pop_failures()`.

## Things to be aware of / remember
- Developed on macOS; it cannot run there. Verify syntax with `python3 -m py_compile` and test
  logic by stubbing `ctypes.WinDLL`/`WINFUNCTYPE` and setting `sys.platform = "win32"` before import.
- User chose Windows + multiple game instances. User wants Python (not C, no CI executable build).
- The first version mapped controller → keystrokes; the user explicitly asked to drop it in favor of
  focus spoofing only (the old version is in git history, commit "Add controller mirror tool...").
- The WinEvent hook is out-of-context, so it only fires while the FocusKeeper thread pumps
  messages (MsgWaitForMultipleObjects + PeekMessage loop). Keep the WINEVENTPROC reference alive.
- The real foreground window is never sent spoof messages; removed targets get deactivate messages.
- Posting order is message-major (each message to all windows) to keep windows in sync.
- Never add Co-authored-by trailers to commits (user's global rule).
