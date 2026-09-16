"""
Package the PyInstaller output for handing over:
  dist/TeamsTranscriber/  +  ui/, models/ecapa, models/vosk-model-small-ru-0.22, journal.json.example, README.md, test wav
  -> dist/TeamsTranscriber-<date>.zip

Run after:  .venv\\Scripts\\pyinstaller --noconfirm TeamsTranscriber.spec
"""
import datetime as dt
import os
import shutil
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist", "TeamsTranscriber")
assert os.path.exists(os.path.join(DIST, "TeamsTranscriber.exe")), "run pyinstaller first"

def copy_tree(src, dst):
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)

copy_tree(os.path.join(HERE, "ui"), os.path.join(DIST, "ui"))
os.makedirs(os.path.join(DIST, "models"), exist_ok=True)
copy_tree(os.path.join(HERE, "models", "ecapa"), os.path.join(DIST, "models", "ecapa"))
copy_tree(os.path.join(HERE, "models", "vosk-model-small-ru-0.22"), os.path.join(DIST, "models", "vosk-model-small-ru-0.22"))
uc = os.path.join(HERE, "unitycapture", "UnityCapture-master", "Install")
if os.path.isdir(uc):
    copy_tree(uc, os.path.join(DIST, "unitycapture", "Install"))
for f in ("journal.json.example", "README.md", "test_meeting.wav", "processor_server.py"):
    if os.path.exists(os.path.join(HERE, f)):
        shutil.copy2(os.path.join(HERE, f), os.path.join(DIST, f))
with open(os.path.join(DIST, "ЧИТАЙ.txt"), "w", encoding="utf-8") as f:
    f.write(
        "Teams transcriber\n\n"
        "1. Запусти TeamsTranscriber.exe. При первом старте программа скачает модель распознавания Whisper (~1,6 ГБ), нужен интернет.\n"
        "2. Настройки (шестерёнка): вход = откуда брать звук. Проще всего нажать «Установить своё устройство» (виртуальный кабель VB-Cable,\n"
        "   нужны права администратора), потом в Teams выбрать вывод «Teams transcriber». Выход = свои наушники.\n"
        "3. Микрофон и имя для оповещений, журнал top-academy (логин/пароль) и ключ API для анализа пары — по желанию.\n"
        "4. Записи складываются в папку recordings рядом с программой: предмет / пара / transcript.txt.\n\n"
        "Требования: Windows 10/11 64-bit, установленный Microsoft Edge WebView2 (есть в Windows 11), 8 ГБ ОЗУ, интернет для первого запуска.\n"
        "Модель тяжёлая для процессора: на слабом ПК в настройках выбери модель medium или small.\n"
    )

# prune obvious dead weight if present
for junk in ("_internal/torch/test", "_internal/torch/include", "_internal/torch/lib/*.lib"):
    pass

stamp = dt.datetime.now().strftime("%Y-%m-%d")
zpath = os.path.join(HERE, "dist", f"TeamsTranscriber-{stamp}.zip")
if os.path.exists(zpath):
    os.remove(zpath)
total = 0
with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for dp, dn, fn in os.walk(DIST):
        for name in fn:
            full = os.path.join(dp, name)
            total += os.path.getsize(full)
            z.write(full, os.path.join("TeamsTranscriber", os.path.relpath(full, DIST)))
print(f"folder: {total / 1e6:.0f} MB, zip: {os.path.getsize(zpath) / 1e6:.0f} MB -> {zpath}")
