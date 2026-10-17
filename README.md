# Controller Mirror

Control several game windows with one Xbox/XInput controller, all at once, even while
they're in the background. Windows only.

## How it works

Windows doesn't route controller input to individual windows: XInput is system-wide,
and most games ignore it when they don't have focus. So Controller Mirror:

1. polls your controller through XInput about 1,000 times a second, and
2. on every change, posts the mapped **key down/up messages** straight into each selected
   window's message queue (`PostMessage`). No focus switching is needed.

Each message goes to every selected window before the next one is sent, so all windows
get the same input within microseconds of each other.

## Run

Requires Python 3.8+ for Windows (python.org build, tkinter included). No packages to install.

```
python controller_mirror.py
```

1. Pick the controller slot (1–4). The label shows whether it's connected.
2. Tick the game windows (click a row or press Space). Use **Filter** to narrow the list and **Refresh** after launching new instances.
3. Press **Start mirroring**. You can tick or untick windows while it runs.

## Key mapping

`mapping.json` is created next to the script on first run. Click **Edit mapping**, save, then **Reload mapping**.

```json
{
  "stick_deadzone": 0.35,
  "trigger_threshold": 0.25,
  "poll_interval_ms": 1,
  "bindings": { "A": "SPACE", "LS_UP": "W", "RT": ["LSHIFT", "G"], "BACK": null }
}
```

- **Inputs:** `A B X Y LB RB LT RT START BACK LS RS DPAD_UP/DOWN/LEFT/RIGHT LS_UP/DOWN/LEFT/RIGHT RS_UP/DOWN/LEFT/RIGHT`
- **Keys:** `A`–`Z`, `0`–`9`, `F1`–`F24`, `NUMPAD0`–`9`, `SPACE ENTER ESC TAB BACKSPACE`,
  `SHIFT/LSHIFT/RSHIFT CTRL/LCTRL/RCTRL ALT/LALT/RALT`, `UP DOWN LEFT RIGHT`,
  `INSERT DELETE HOME END PAGEUP PAGEDOWN`, `MINUS EQUALS COMMA PERIOD SLASH SEMICOLON QUOTE GRAVE LBRACKET RBRACKET BACKSLASH`,
  or a raw virtual-key code such as `"0x41"`.
- A binding can be one key, a list of keys pressed together, or `null`.
- Sticks act as digital directions (eight-way). Analog strength isn't kept.

## Limitations

- **The game must read keyboard *messages*** (`WM_KEYDOWN`), and most do. Games that read the
  keyboard only through Raw Input, DirectInput, or `GetAsyncKeyState` ignore posted messages, and
  this method can't reach them in the background.
- **Games that pause or drop input when unfocused:** turn on **Spoof focus**. It sends
  activate/focus messages to the selected windows when mirroring starts and when windows are
  added. If you click into a window afterwards, toggle the box off and on again.
- **Elevated games:** if a game runs as administrator, run Controller Mirror as administrator too.
  Otherwise Windows blocks the messages, and the status bar tells you so.
- **The focused game also reads the real controller.** If it gets input twice, untick it, or turn
  off its native controller support.
