# PyInstaller spec for florr-auto-pathing.
#
# Build ON THE TARGET OS — PyInstaller does not cross-compile. For a Windows
# .exe this must run on the Windows machine (see build_windows.bat).
#
# maps/ is deliberately NOT bundled as --add-data: the code reads it via a
# relative path ("./maps/...") resolved against the process's cwd, which for
# a double-clicked exe is the exe's own folder. Bundling it into PyInstaller's
# internal _internal/ dir (default since PyInstaller 6.0) would break that
# lookup. The build script copies maps/ next to the built exe instead.
#
# canvas_hook.js IS bundled: cdp_bridge.py loads it relative to its OWN module
# file (Path(__file__), or sys._MEIPASS when frozen), not the cwd — so it must
# ride inside _internal/ next to the frozen modules, which is where a datas
# entry with dest "." lands and where cdp_bridge's frozen branch looks.

block_cipher = None

datas = [("canvas_hook.js", ".")]
binaries = []
hiddenimports = [
    "pyautogui", "pyscreeze", "pymsgbox", "pytweening", "pygetwindow", "mouseinfo",
    "PIL", "PIL._tkinter_finder", "websocket", "certifi", "tkinter", "customtkinter",
]

from PyInstaller.utils.hooks import collect_all

# cv2 + numpy are pulled in transitively (utils.py's maze recognition).
for pkg in ("cv2", "customtkinter"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="florr-auto-pathing",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,   # 主入口是 GUI 窗口; worker 子进程的日志由 GUI 用 Popen(stdout=PIPE) 收, 不需要控制台
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="florr-auto-pathing",
)
