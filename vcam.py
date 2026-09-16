"""
Virtual camera fed from a video file: the file plays in a loop into a virtual webcam device that Teams (or anything)
can pick as its camera. Needs a virtual camera driver on the machine: "OBS Virtual Camera" (comes with OBS Studio)
or "Unity Video Capture" (UnityCapture, a 1 MB DirectShow filter). pyvirtualcam talks to either.

    vc = VirtualCam(); vc.start(r"C:\\video.mp4"); vc.stop(); vc.status()
"""

import os
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

from paths import BASE_DIR

UNITY_URL = "https://github.com/schellingb/UnityCapture/archive/refs/heads/master.zip"
OUT_W, OUT_H = 1280, 720


def available_backends():
    """Which virtual camera devices exist right now: list of (backend, device name)."""
    out = []
    try:
        import pyvirtualcam
    except Exception:
        return out
    for b in ("obs", "unitycapture"):
        try:
            cam = pyvirtualcam.Camera(width=320, height=180, fps=10, backend=b, print_fps=False)
            out.append((b, cam.device)); cam.close()
        except Exception:
            pass
    return out


class VirtualCam:
    def __init__(self, on_log=None, on_state=None):
        self.on_log = on_log or (lambda m: None)
        self.on_state = on_state
        self._thread = None
        self._stop = threading.Event()
        self.file = None
        self.device = None
        self.error = None
        self.frames = 0
        self.fps = 0.0

    # ---------------------------------------------------------------- control
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def status(self):
        return {"on": self.running(), "file": self.file, "device": self.device, "error": self.error, "frames": self.frames, "fps": round(self.fps, 1)}

    def start(self, file):
        if self.running():
            self.stop()
        if not file or not os.path.exists(file):
            self.error = "файл видео не найден"; self._emit(); return False
        self.file, self.error, self.frames = file, None, 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3)
        self._thread = None
        self._emit()

    def _emit(self):
        if self.on_state:
            try:
                self.on_state(self.status())
            except Exception:
                pass

    # ---------------------------------------------------------------- loop
    def _run(self):
        try:
            import cv2
            import numpy as np
            import pyvirtualcam
            from pyvirtualcam import PixelFormat
        except Exception as e:
            self.error = f"нет библиотек: {e}"; self._emit(); return
        cap = cv2.VideoCapture(self.file)
        if not cap.isOpened():
            self.error = "не удалось открыть видео"; self._emit(); return
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        fps = min(60.0, max(5.0, fps))
        self.fps = fps
        cam = None
        for backend in ("obs", "unitycapture"):
            try:
                cam = pyvirtualcam.Camera(width=OUT_W, height=OUT_H, fps=fps, fmt=PixelFormat.BGR, backend=backend, print_fps=False)
                break
            except Exception as e:
                last = e
        if cam is None:
            self.error = "нет виртуальной камеры (нужен OBS Virtual Camera или Unity Capture)"
            self.on_log(f"[vcam] {self.error}: {last}")
            cap.release(); self._emit(); return
        self.device = cam.device
        self.on_log(f"[vcam] камера включена: {cam.device}, файл {os.path.basename(self.file)}, {fps:.0f} fps")
        self._emit()
        canvas = np.zeros((OUT_H, OUT_W, 3), dtype=np.uint8)
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop
                    ok, frame = cap.read()
                    if not ok:
                        break
                h, w = frame.shape[:2]
                scale = min(OUT_W / w, OUT_H / h)
                nw, nh = int(w * scale), int(h * scale)
                if (nw, nh) != (w, h):
                    frame = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
                canvas[:] = 0
                y0, x0 = (OUT_H - nh) // 2, (OUT_W - nw) // 2
                canvas[y0:y0 + nh, x0:x0 + nw] = frame
                cam.send(canvas)
                self.frames += 1
                cam.sleep_until_next_frame()
        except Exception as e:
            self.error = str(e)
            self.on_log(f"[vcam] ошибка: {e}")
        finally:
            cap.release()
            try:
                cam.close()
            except Exception:
                pass
            self.on_log("[vcam] камера выключена")
            self._emit()


# ---------------------------------------------------------------- physical cameras: list + record a clip
VIRTUAL_HINTS = ("manycam", "unity video capture", "obs virtual", "obs-camera", "snap camera", "xsplit", "droidcam")


def list_cameras(virtual_name=None):
    """DirectShow video devices as [{index, name, virtual}] (index = OpenCV CAP_DSHOW index)."""
    try:
        from pygrabber.dshow_graph import FilterGraph
        names = FilterGraph().get_input_devices()
    except Exception:
        return []
    out = []
    for i, n in enumerate(names):
        low = n.lower()
        virtual = any(h in low for h in VIRTUAL_HINTS) or (virtual_name and virtual_name.lower() in low)
        out.append({"index": i, "name": n, "virtual": bool(virtual)})
    return out


class CamRecorder(threading.Thread):
    """Record N seconds from a physical webcam into an mp4 (for the virtual camera loop)."""

    def __init__(self, index, seconds, out_path, on_progress=None, width=1280, height=720):
        super().__init__(daemon=True)
        self.index, self.seconds, self.out_path = int(index), max(3, int(seconds)), out_path
        self.on_progress = on_progress or (lambda st: None)
        self.width, self.height = width, height
        self.result = None

    def run(self):
        import cv2
        cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            self.result = {"ok": False, "error": "камера не открывается: занята другим приложением (Teams, ManyCam) или отключена"}
            self.on_progress(dict(self.result, state="error")); return
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))  # USB2 webcams give 30 fps at 720p only in MJPEG
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width); cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, 30)
        ok, frame = False, None
        for _ in range(30):
            ok, frame = cap.read()
            if ok:
                break
            time.sleep(0.1)
        if not ok:
            cap.release()
            self.result = {"ok": False, "error": "камера не отдаёт кадры (занята другим приложением?)"}
            self.on_progress(dict(self.result, state="error")); return
        h, w = frame.shape[:2]
        # measure the real frame rate for two seconds so the clip plays at natural speed
        t0 = time.time(); n = 0
        while time.time() - t0 < 2.0:
            if cap.read()[0]:
                n += 1
        fps = max(5, min(30, round(n / 2.0)))
        os.makedirs(os.path.dirname(self.out_path), exist_ok=True)
        wr = cv2.VideoWriter(self.out_path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        self.on_progress({"state": "recording", "left": self.seconds, "total": self.seconds, "size": f"{w}x{h}", "fps": fps})
        t0 = time.time(); frames = 0; last_tick = -1
        try:
            while True:
                el = time.time() - t0
                if el >= self.seconds:
                    break
                ok, frame = cap.read()
                if ok:
                    wr.write(frame); frames += 1
                tick = int(el)
                if tick != last_tick:
                    last_tick = tick
                    self.on_progress({"state": "recording", "left": self.seconds - tick, "total": self.seconds, "size": f"{w}x{h}", "fps": fps})
        finally:
            wr.release(); cap.release()
        self.result = {"ok": True, "file": self.out_path, "frames": frames, "fps": fps, "seconds": self.seconds}
        self.on_progress(dict(self.result, state="done"))


# ---------------------------------------------------------------- driver install (Unity Capture)
DEFAULT_NAME = "HD Web Camera"


def install_unity_capture(log=print, name=None):
    """Register the UnityCapture DirectShow filter under a custom device name (UAC prompt). Re-running with another
    name re-registers the filter: the old device disappears, the new one appears (Teams lists it under that name)."""
    dest = os.path.join(BASE_DIR, "unitycapture")
    os.makedirs(dest, exist_ok=True)
    dll = None
    for dp, dn, fn in os.walk(dest):  # bundled with the exe? then no download
        for n in fn:
            if n.lower() in ("unitycapturefilter64.dll", "unitycapturefilter64bit.dll"):
                dll = os.path.join(dp, n)
    if dll is None:
        zpath = os.path.join(dest, "UnityCapture.zip")
        log(f"скачиваю {UNITY_URL}")
        req = urllib.request.Request(UNITY_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as r, open(zpath, "wb") as f:
            f.write(r.read())
        with zipfile.ZipFile(zpath) as z:
            z.extractall(dest)
    for dp, dn, fn in os.walk(dest):
        for n in fn:
            if n.lower() in ("unitycapturefilter64.dll", "unitycapturefilter64bit.dll"):
                dll = os.path.join(dp, n)
    if not dll:
        raise RuntimeError("в архиве нет UnityCaptureFilter64.dll")
    name = (name or DEFAULT_NAME).strip().replace('"', "").replace("'", "")[:60] or DEFAULT_NAME
    log(f"регистрирую камеру «{name}» (запрос прав администратора): {dll}")
    # one elevated cmd script (quoting with spaces in the name is only reliable this way):
    # unregister the previous device, register the filter under the new name
    script = os.path.join(dest, "register.cmd")
    with open(script, "w", encoding="cp866", errors="replace") as f:
        f.write("@echo off\r\n")
        dll32 = os.path.join(os.path.dirname(dll), "UnityCaptureFilter32.dll")
        for d in ([dll32] if os.path.exists(dll32) else []) + [dll]:   # 32-bit filter too: 32-bit apps enumerate that view
            f.write(f'regsvr32 /s /u "{d}"\r\n')
            f.write(f'regsvr32 /s "/i:UnityCaptureName={name}" "{d}"\r\n')
    ps = f"Start-Process cmd.exe -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '/c','\"{script}\"'"
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(1)
    found = available_backends()
    log("готово: " + (", ".join(d for _, d in found) if found else "устройство пока не видно, попробуй перезапустить программу"))
    return found


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        install_unity_capture(name=sys.argv[2] if len(sys.argv) > 2 else None)
    elif len(sys.argv) > 1:
        vc = VirtualCam(on_log=print); vc.start(sys.argv[1]); time.sleep(15); print(vc.status()); vc.stop()
    else:
        print("backends:", available_backends())
