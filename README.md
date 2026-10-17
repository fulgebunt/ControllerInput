# Controller Mirror

Lets several game windows read your **real controller** at once, even while they're in the
background. Windows only.

## How it works

Windows already sends controller input to every program. Most games simply ignore it when
they aren't the active window. Controller Mirror keeps telling every selected window that it
*is* the active window (`WM_ACTIVATEAPP` / `WM_ACTIVATE` / `WM_SETFOCUS`), so each instance keeps
reading the controller itself: analog sticks, triggers, everything, at the same instant.

- When the real foreground window changes, every selected window is re-activated straight away.
- On top of that, activation is re-sent on a short interval (100 ms by default, adjustable in the UI).
- The window that really is in the foreground is left alone.
- Unticking a window, pressing Stop, or closing the tool tells those windows they're inactive again.

## Run

Requires Python 3.8+ for Windows (python.org build, tkinter included). No packages to install.

```
python controller_mirror.py
```

1. Tick the game windows (click a row or press Space). Use **Filter** to narrow the list and
   **Refresh** after launching new instances.
2. Press **Start**. You can tick or untick windows while it runs.

## Limitations

- **This only helps games that learn about focus from window messages.** Games that ask Windows
  directly (`GetForegroundWindow`, `GetActiveWindow`) still know they're in the background.
  Those need hooking inside the game (for example Nucleus Co-op), which this tool doesn't do.
- **SDL-based games** drop background controller input on their own. They often respect the
  environment variable `SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1` if it's set before the game is launched:
  ```
  set SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1
  start "" "C:\path\to\game.exe"
  ```
- **Elevated games:** if a game runs as administrator, run Controller Mirror as administrator too.
  Otherwise Windows blocks the messages, and the status bar tells you so.
- A game that thinks it's focused may also react to your keyboard and mouse, or grab the
  cursor, depending on the game.
