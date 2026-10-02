# Build on an Apple Silicon Mac: python -m PyInstaller --noconfirm TeamsTranscriber-macos.spec
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [("ui", "ui"), ("test_meeting.wav", "."), ("journal.json.example", ".")]
if os.path.isdir("models/vosk-model-small-ru-0.22"):
    datas.append(("models/vosk-model-small-ru-0.22", "models/vosk-model-small-ru-0.22"))
binaries = []
hiddenimports = []
for pkg in ("faster_whisper", "ctranslate2", "silero_vad", "speechbrain", "vosk",
            "webview", "torchaudio", "huggingface_hub", "tokenizers", "sentencepiece",
            "hyperpyyaml", "ruamel.yaml", "pyvirtualcam", "cv2", "docx", "reportlab"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print("collect_all skipped:", pkg, e)
hiddenimports += collect_submodules("webview.platforms.cocoa")
hiddenimports += ["journal", "teams_mic", "alerts", "analyst", "vcable", "transcribe",
                  "paths", "vcam", "slides", "report", "notify", "speechbrain.inference.speaker"]

a = Analysis(["app.py"], pathex=["."], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, hookspath=[],
             excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "winotify",
                       "pyaudiowpatch", "uiautomation", "comtypes", "pycaw", "pygrabber"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TeamsTranscriber",
          console=False, target_arch="arm64", upx=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TeamsTranscriber")
app = BUNDLE(coll, name="TeamsTranscriber.app", bundle_identifier="io.github.lolkofka.teamstranscriber",
             info_plist={"NSMicrophoneUsageDescription": "Запись звука для расшифровки встреч",
                         "NSCameraUsageDescription": "Запись видео для виртуальной камеры"})
