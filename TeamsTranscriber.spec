# PyInstaller spec: onedir build of the desktop app.
#   .venv\Scripts\pyinstaller --noconfirm TeamsTranscriber.spec
# Result: dist\TeamsTranscriber\  (exe + _internal); build.py copies ui/, models/ and docs next to the exe.
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []
for pkg in ("faster_whisper", "ctranslate2", "speechbrain", "silero_vad", "vosk", "webview", "uiautomation",
            "torchaudio", "comtypes", "pycaw", "pyvirtualcam", "cv2", "docx", "reportlab", "winotify", "mss", "huggingface_hub", "tokenizers", "sentencepiece", "hyperpyyaml", "ruamel.yaml"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception as e:  # optional packages
        print("collect_all skipped:", pkg, e)
hiddenimports += collect_submodules("pyaudiowpatch")
hiddenimports += ["journal", "teams_mic", "alerts", "analyst", "vcable", "vcable_rename", "transcribe", "paths", "vcam", "slides", "report", "notify",
                  "speechbrain.inference.speaker", "torchaudio.functional", "silero_vad.utils_vad"]

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "notebook", "scipy.spatial.cKDTree"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="TeamsTranscriber",
    icon="ui/icon.ico",
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TeamsTranscriber")
