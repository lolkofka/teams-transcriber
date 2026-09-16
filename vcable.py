"""
Own virtual audio device for the transcriber.

Windows only lets a signed kernel driver create an audio endpoint, so we install the free VB-Audio Virtual Cable
(https://vb-audio.com/Cable/) and present it as ours:
  - Teams/Discord output  -> "CABLE Input"  (render endpoint, renamed to "Teams transcriber")
  - transcriber input     <- "CABLE Output" (capture endpoint, renamed to "Teams transcriber (захват)")

install():  download the driver pack, run its installer (UAC prompt), then rename the endpoints.
find():     locate the cable endpoints among WASAPI devices (by our name or the stock CABLE name).
rename_endpoints(): registry keys under MMDevices are not writable even for SYSTEM on current Windows 11, so the
                    rename goes through IMMDevice.OpenPropertyStore(STGM_READWRITE) from an elevated helper
                    (vcable_rename.py), exactly what the Sound control panel does.
"""

import ctypes
import os
import subprocess
import sys
import urllib.request
import zipfile

from paths import BASE_DIR, FROZEN

VB_URL = "https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip"
OUR_RENDER = "Teams transcriber"
OUR_CAPTURE = "Teams transcriber (захват)"
STOCK_RENDER = "CABLE Input"
STOCK_CAPTURE = "CABLE Output"


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find(inputs=None, outputs=None):
    """Return (capture_device, render_device) dicts from transcribe.list_* or None each."""
    import transcribe as T
    inputs = inputs if inputs is not None else T.list_input_devices()
    outputs = outputs if outputs is not None else T.list_output_devices()
    cap = next((d for d in inputs if not d["loopback"] and (OUR_CAPTURE in d["name"] or STOCK_CAPTURE in d["name"])), None)
    ren = next((d for d in outputs if OUR_RENDER in d["name"] or STOCK_RENDER in d["name"]), None)
    return cap, ren


def download(dest_dir, log=print):
    os.makedirs(dest_dir, exist_ok=True)
    zpath = os.path.join(dest_dir, "VBCABLE_Driver_Pack.zip")
    log(f"скачиваю {VB_URL}")
    req = urllib.request.Request(VB_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(zpath, "wb") as f:
        f.write(r.read())
    with zipfile.ZipFile(zpath) as z:
        z.extractall(dest_dir)
    exe = os.path.join(dest_dir, "VBCABLE_Setup_x64.exe")
    if not os.path.exists(exe):
        raise RuntimeError("в архиве нет VBCABLE_Setup_x64.exe")
    log(f"распаковано: {exe} ({os.path.getsize(zpath) // 1024} КБ)")
    return exe


def run_installer(exe, log=print):
    """Launch the VB-Cable installer elevated; the user clicks 'Install Driver' in its window."""
    log("запускаю установщик (запрос прав администратора, в окне нажми Install Driver)")
    ps = f'Start-Process -FilePath "{exe}" -Verb RunAs -Wait'
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)


def rename_endpoints(log=print):
    """Rename the cable endpoints to our names the way the Sound control panel does (IPropertyStore.SetValue).
    Needs an elevated process, so vcable_rename.py is launched via UAC and reports through vcable_rename.log."""
    here = BASE_DIR
    logp = os.path.join(here, "vcable_rename.log")
    try:
        os.remove(logp)
    except OSError:
        pass
    if FROZEN:  # the exe itself handles --rename-cable (see app.py)
        py = sys.executable
        args = f"'--rename-cable','\"{STOCK_RENDER}={OUR_RENDER}\"','\"{STOCK_CAPTURE}={OUR_CAPTURE}\"'"
    else:
        py = sys.executable.replace("pythonw.exe", "python.exe")
        args = f"'-X','utf8','vcable_rename.py','\"{STOCK_RENDER}={OUR_RENDER}\"','\"{STOCK_CAPTURE}={OUR_CAPTURE}\"'"
    ps = (f"Start-Process -FilePath '{py}' -ArgumentList {args} -WorkingDirectory '{here}' -Verb RunAs -Wait")
    log("переименовываю устройства кабеля (запрос прав администратора)")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        with open(logp, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    log(line.rstrip())
    except OSError:
        log("переименование не выполнено (отказ в UAC или ошибка запуска)")


def install(log=print):
    here = BASE_DIR
    exe = download(os.path.join(here, "vbcable"), log)
    run_installer(exe, log)
    rename_endpoints(log)
    cap, ren = find()
    if cap and ren:
        log(f"готово: вход программы «{cap['name']}», вывод для Teams «{ren['name']}»")
    else:
        log("устройства кабеля пока не видны: перезагрузи компьютер (драйвер требует), потом нажми Обновить")
    return cap, ren


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rename":
        rename_endpoints()
    else:
        install()
