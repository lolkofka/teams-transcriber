"""
Slide capture: while someone shares their screen in the Teams call, grab the shared-content stage every few seconds
and keep a frame only when it visibly changed (perceptual dHash, Hamming distance above a threshold).

The stage is found through UI Automation: the call window (class TeamsWebView) contains an element named
"Общий контент от пользователя <имя>" / "Shared content from <name>" whose bounding rectangle is the shared picture.
Frames are taken with PrintWindow only (renders the Teams window itself even when other windows cover it; nothing
when minimized). There is deliberately no screen-grab fallback: it would capture whatever lies on top of Teams.

    cap = SlideCapture(out_dir, interval=3, threshold=10, on_slide=cb, on_log=print); cap.start(); cap.stop()
Files: <out_dir>/slides/HH-MM-SS.png + <out_dir>/slides.jsonl  {t, file, presenter, w, h}
"""

import ctypes
import ctypes.wintypes as wt
import warnings
warnings.filterwarnings("ignore")
import json
import os
import threading
import time

STAGE_PREFIXES = ("Общий контент от пользователя", "Shared content from", "Общий контент")


# ------------------------------------------------------------------------------------------------ window capture
def _print_window(hwnd, rect):
    """PrintWindow(PW_RENDERFULLCONTENT) -> PIL image of the whole window, or None."""
    from PIL import Image
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        return None
    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = gdi32.SelectObject(hdc_mem, bmp)
    ok = user32.PrintWindow(hwnd, hdc_mem, 2)  # PW_RENDERFULLCONTENT
    img = None
    if ok:
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]
        bi = BITMAPINFOHEADER(); bi.biSize = ctypes.sizeof(BITMAPINFOHEADER); bi.biWidth = w; bi.biHeight = -h
        bi.biPlanes = 1; bi.biBitCount = 32; bi.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        if got:
            img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    gdi32.SelectObject(hdc_mem, old); gdi32.DeleteObject(bmp); gdi32.DeleteDC(hdc_mem); user32.ReleaseDC(hwnd, hdc_win)
    return img


def _screen_grab(rect):
    import mss
    from PIL import Image
    left, top, right, bottom = rect
    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (1 if px[r * (size + 1) + c] > px[r * (size + 1) + c + 1] else 0)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


# ------------------------------------------------------------------------------------------------ stage lookup
class StageFinder:
    """UI Automation lookup of the shared-content stage inside the Teams call window (call from one thread)."""

    def __init__(self):
        import uiautomation as auto
        from uiautomation import uiautomation as U
        auto.InitializeUIAutomationInCurrentThread()
        self.auto = auto
        self.ui = U._AutomationClient.instance().IUIAutomation
        self.cond_menu = self.ui.CreatePropertyCondition(auto.PropertyId.ControlTypeProperty, auto.ControlType.MenuItemControl)

    def find(self):
        """-> dict(hwnd, win_rect, stage_rect, presenter) or None."""
        for w in self.auto.GetRootControl().GetChildren():
            if w.ClassName != "TeamsWebView":
                continue
            try:
                arr = w.Element.FindAll(4, self.cond_menu)  # TreeScope_Descendants
            except Exception:
                continue
            for i in range(arr.Length):
                el = arr.GetElement(i)
                name = el.CurrentName or ""
                if not any(name.startswith(p) for p in STAGE_PREFIXES):
                    continue
                r = el.CurrentBoundingRectangle
                stage = (int(r.left), int(r.top), int(r.right), int(r.bottom))
                if stage[2] - stage[0] < 200 or stage[3] - stage[1] < 150:
                    continue
                wr = w.BoundingRectangle
                import re
                presenter = re.sub(r"^(общий контент|shared content)\s*(от пользователя|from)?\s*", "", name, flags=re.I).strip(" :,") or name
                return {"hwnd": w.NativeWindowHandle, "win_rect": (wr.left, wr.top, wr.right, wr.bottom),
                        "stage_rect": stage, "presenter": presenter, "title": w.Name}
        return None


def grab_stage(info):
    """PIL image of the shared content, taken ONLY from the Teams window itself (PrintWindow).
    No screen grabs: a screen grab of the rectangle would capture whatever window lies on top (Telegram, browser...)."""
    hwnd, wr, sr = info["hwnd"], info["win_rect"], info["stage_rect"]
    if ctypes.windll.user32.IsIconic(hwnd):
        return None  # minimized: nothing is rendered
    try:
        full = _print_window(hwnd, wr)
    except Exception:
        return None
    if full is None:
        return None
    box = (max(0, sr[0] - wr[0]), max(0, sr[1] - wr[1]), min(full.width, sr[2] - wr[0]), min(full.height, sr[3] - wr[1]))
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    img = full.crop(box)
    if img.convert("L").getextrema()[1] < 8:
        return None  # not rendered this time: skip the frame rather than grab the screen
    return img


# ------------------------------------------------------------------------------------------------ capture thread
class SlideCapture(threading.Thread):
    def __init__(self, out_dir, interval=3.0, threshold=10, on_slide=None, on_log=None, on_state=None, max_width=1600):
        super().__init__(daemon=True)
        self.out_dir = out_dir
        self.interval = max(1.0, float(interval))
        self.threshold = int(threshold)
        self.on_slide = on_slide
        self.on_log = on_log or (lambda m: None)
        self.on_state = on_state
        self.max_width = max_width
        self._stop_ev = threading.Event()
        self.last_hash = None
        self.candidate = None      # (hash, img) seen once, saved when seen twice in a row (skips transitions)
        self.count = 0
        self.sharing = False
        self.presenter = None

    def stop(self):
        self._stop_ev.set()

    def run(self):
        try:
            finder = StageFinder()
        except Exception as e:
            self.on_log(f"[slides] UI Automation недоступен: {e}")
            return
        os.makedirs(os.path.join(self.out_dir, "slides"), exist_ok=True)
        idx_path = os.path.join(self.out_dir, "slides.jsonl")
        # resume numbering / last hash from a previous run in the same lesson folder
        try:
            with open(idx_path, encoding="utf-8") as f:
                for line in f:
                    d = json.loads(line); self.count += 1; self.last_hash = int(d.get("hash", "0"), 16) if d.get("hash") else self.last_hash
        except Exception:
            pass
        while not self._stop_ev.is_set():
            t0 = time.time()
            try:
                info = finder.find()
                sharing = info is not None
                if sharing != self.sharing or (info and info["presenter"] != self.presenter):
                    self.sharing, self.presenter = sharing, (info["presenter"] if info else None)
                    self.on_log(f"[slides] показ экрана {'начался: ' + self.presenter if sharing else 'закончился'}")
                    if self.on_state:
                        self.on_state({"sharing": sharing, "presenter": self.presenter})
                if info:
                    img = grab_stage(info)
                    if img is not None:
                        self._consider(img, info)
            except Exception as e:
                self.on_log(f"[slides] ошибка захвата: {e}")
            self._stop_ev.wait(max(0.2, self.interval - (time.time() - t0)))

    def _consider(self, img, info):
        h = dhash(img)
        if self.last_hash is not None and hamming(h, self.last_hash) <= self.threshold:
            self.candidate = None
            return
        # new picture: require two consecutive similar frames so we don't keep mid-transition frames
        if self.candidate is None or hamming(h, self.candidate[0]) > self.threshold:
            self.candidate = (h, img)
            return
        self.candidate = None
        self._save(img, h, info)

    def _save(self, img, h, info):
        if img.width > self.max_width:
            img = img.resize((self.max_width, int(img.height * self.max_width / img.width)))
        now = time.time()
        name = time.strftime("%H-%M-%S", time.localtime(now)) + ".png"
        path = os.path.join(self.out_dir, "slides", name)
        img.save(path, optimize=True)
        self.last_hash = h
        self.count += 1
        rec = {"t": round(now, 2), "wall": time.strftime("%H:%M:%S", time.localtime(now)), "file": os.path.join("slides", name),
               "presenter": info.get("presenter"), "w": img.width, "h": img.height, "hash": f"{h:016x}"}
        with open(os.path.join(self.out_dir, "slides.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.on_log(f"[slides] сохранён слайд {self.count}: {name}")
        if self.on_slide:
            self.on_slide(dict(rec, path=path, n=self.count))


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "slides_test"
    cap = SlideCapture(out, interval=2, threshold=10, on_slide=lambda r: print("slide:", r["file"], r["w"], "x", r["h"]), on_log=print)
    cap.start()
    time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 20)
    cap.stop(); cap.join(3)
    print("saved:", cap.count, "| sharing:", cap.sharing, cap.presenter)
