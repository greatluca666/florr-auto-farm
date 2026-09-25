# Packaging into an .exe

Build **on Windows** — PyInstaller doesn't cross-compile, so running this on
a Mac produces a macOS binary, not a `.exe`. (This repo's
`main.spec` was sanity-checked on macOS to confirm the import graph resolves
cleanly; the actual `.exe` still has to come from a Windows build.)

## Steps (on the Windows machine)

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
build_windows.bat
```

(`build_windows.bat` looks for `pyinstaller` on PATH, so run it from a shell
where the venv is active.)

This runs PyInstaller against [main.spec](main.spec) and produces
`dist\florr-auto-pathing\florr-auto-pathing.exe` (one-dir build — not
one-file, so startup stays fast and nothing has to re-extract on every
launch). Enemy detection decodes the game's canvas draw calls, so there is
no model file to ship.

The script also copies `maps\` next to the exe. There is **no** `models\`
step any more — enemy detection needs no weights file.

## Why not bundle maps via PyInstaller `--add-data`

`utils.py`/`enemy_detect.py` open `maps/…` as paths relative to the
process's cwd (`./maps/desert.png`). For a double-clicked exe, cwd is the
exe's own folder — but PyInstaller's `--add-data` extracts into its internal
`_internal\` subfolder (default since PyInstaller 6.0), not the top-level
dist folder, so a relative `./maps` lookup wouldn't find it. Copying `maps\`
next to the exe after the build keeps the exact same relative-path behavior
the unpackaged `python main.py` already relies on, with no code changes.

## Running the built exe

Double-click `florr-auto-pathing.exe` to open the control-panel GUI window
(no black console window appears — `console=False` in main.spec). The GUI has
two pages:

- **时间表** — a list of weekly time blocks (weekdays + time range + account +
  map + target/area + toggles). Click **▶ 开始调度** to let the GUI drive the
  bot by the plan.
- **账号** — manage one Chrome profile per florr.io account
  (`chrome-profiles\<别名>\`).

When a time block becomes active, the GUI (no dialogs, no interaction):

1. Stops the current worker (if any).
2. If the block's account differs from the running Chrome, force-closes Chrome
   and relaunches it on that account's profile dir with the CDP flags +
   `--start-fullscreen`, opening `https://florr.io`. If that profile has never
   been logged in (no florr.io tab within 30s), the block is skipped with a log
   line — the schedule is never blocked waiting on a human.
3. Writes the block's farming params into `config.json` (`active` slice).
4. Spawns a worker subprocess (`python main.py --worker`) for the
   pathfinding/farming loop. The worker clicks the in-game start button and
   handles death screens on its own.

The only interactive flow is **新建账号 / 重新登录** on the 账号 page: Chrome
opens florr.io in a normal window, a non-modal guide panel appears in the GUI
(the window stays minimizable), and you click 完成 once logged in.

The direct `python main.py --worker` path only verifies a ready Chrome exists
(`cdp_bridge.is_dedicated_chrome_ready()`); if not ready it exits with an error
and does not prompt.

Worker logs stream into the GUI's log box in real time (no separate console
window, thanks to `console=False`).

Configuration persists in `config.json` next to the exe (schema v2:
`profiles` + `schedule` + `active` + `afk_enabled`). An old flat v1 config is
migrated on first launch. On first run with no file, the app uses built-in
defaults.

A `chrome-profiles\` folder (one subdir per account) appears next to the exe
after first use — each holds a live florr.io session. Do **not** include it
when zipping the exe folder to distribute it.

## Distributing it

The dist folder is self-contained (bundles its own Python + all deps) — anyone
running it needs no Python install. Zip `dist\florr-auto-pathing\` as a whole;
the exe will not run correctly if separated from its `_internal\` folder.
