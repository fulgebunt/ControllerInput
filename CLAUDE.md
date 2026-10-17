# Controller Mirror

Windows-only, single-file Python tool (`controller_mirror.py`, stdlib only: ctypes + tkinter)
that reads an XInput controller and posts mapped WM_KEYDOWN/WM_KEYUP messages to multiple
selected windows (background-capable). See README.md for usage.

## Structure
- Win32/XInput ctypes bindings → key-name parsing (`parse_key` builds ready-made message tuples)
  → `Config` (from `mapping.json`) → `Mirror` thread (poll loop, diffs held keys, posts batches)
  → `App` tkinter UI.
- UI thread talks to the `Mirror` thread only via plain attribute swaps (`targets`, `config`,
  `enabled`, `user_index`, `spoof_focus`) and `pop_failures()`.

## Things to be aware of / remember
- Developed on macOS; it cannot run there. Verify syntax with `python3 -m py_compile` and test
  logic by stubbing `ctypes.WinDLL`/`WINFUNCTYPE` and setting `sys.platform = "win32"` before import.
- User chose Windows + multiple game instances as the target (keystroke injection approach,
  not virtual gamepads / DLL hooking).
- `mapping.json` is generated on first run and gitignored (per-user bindings).
- Posting order is message-major (each message to all windows) to keep windows in sync.
- On target-set changes, removed windows receive key-ups for held keys and added windows
  receive key-downs, so no keys get stuck.
- Never add Co-authored-by trailers to commits (user's global rule).
