"""
Name alert: when someone in the call says your name, play a chime on the chosen (or default) output device.

    a = NameAlert(["Кирилл", "Кир"], device_index=None)   # None = system default output
    hit = a.check("Кирилл, ты с нами?")  -> "Кирилл" (or None); plays the chime with an 8 s cooldown
"""

import re
import threading
import time

import numpy as np


def _chime(rate):
    """Two rising tones with a soft envelope, ~0.45 s, stereo float32."""
    out = []
    for f, d in ((880.0, 0.16), (1318.5, 0.26)):
        t = np.arange(int(rate * d)) / rate
        env = np.minimum(1.0, t / 0.015) * np.exp(-t * 9.0)
        out.append(0.35 * np.sin(2 * np.pi * f * t) * env)
        out.append(np.zeros(int(rate * 0.03)))
    x = np.concatenate(out).astype(np.float32)
    return np.stack([x, x], axis=1)


class NameAlert:
    def __init__(self, names, device_index=None, cooldown=8.0, on_hit=None):
        self.set_names(names)
        self.device_index = device_index
        self.cooldown = cooldown
        self.on_hit = on_hit
        self.last_hit = 0.0
        self.enabled = True

    def set_names(self, names):
        stems = [n.strip().lower() for n in (names or []) if n and n.strip()]
        self.stems = sorted(set(stems), key=len, reverse=True)
        # a word starting with the name (declensions: Кирилл/Кирилла/Кириллу); short stems must match exactly
        pats = []
        for st in self.stems:
            if len(st) >= 4:
                pats.append(re.escape(st) + r"[а-яёa-z]{0,3}")
            else:
                pats.append(re.escape(st))
        self.rx = re.compile(r"(?<![а-яёa-z])(" + "|".join(pats) + r")(?![а-яёa-z])", re.I) if pats else None

    def check(self, text, source="teams"):
        """Return the matched word if the text calls you; plays the chime (rate-limited)."""
        if not self.enabled or not self.rx or not text or source == "mic":
            return None
        m = self.rx.search(text)
        if not m:
            return None
        word = m.group(1)
        now = time.time()
        if now - self.last_hit >= self.cooldown:
            self.last_hit = now
            self.play()
            if self.on_hit:
                try:
                    self.on_hit(word, text)
                except Exception:
                    pass
        return word

    def play(self):
        threading.Thread(target=self._play, daemon=True).start()

    def _play(self):
        try:
            import pyaudiowpatch as pyaudio
            from transcribe import PA_LOCK
            with PA_LOCK:
                p = pyaudio.PyAudio()
                try:
                    if self.device_index is None:
                        idx = p.get_host_api_info_by_type(pyaudio.paWASAPI)["defaultOutputDevice"]
                    else:
                        idx = int(self.device_index)
                    d = p.get_device_info_by_index(idx)
                    rate = int(d["defaultSampleRate"])
                    stereo = _chime(rate)
                    stream = None
                    for ch in dict.fromkeys([int(d["maxOutputChannels"]), 2, 1]):
                        try:
                            stream = p.open(format=pyaudio.paFloat32, channels=ch, rate=rate, output=True, output_device_index=idx)
                            break
                        except Exception:
                            stream = None
                    if stream is None:
                        return
                    if ch == 1:
                        buf = stereo[:, :1]
                    elif ch == 2:
                        buf = stereo
                    else:
                        buf = np.concatenate([stereo, np.zeros((len(stereo), ch - 2), np.float32)], axis=1)
                    stream.write(np.ascontiguousarray(buf).tobytes())
                    time.sleep(0.05)
                    stream.stop_stream(); stream.close()
                finally:
                    p.terminate()
        except Exception:
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONASTERISK)
            except Exception:
                pass


if __name__ == "__main__":
    a = NameAlert(["Кирилл"])
    print(a.check("Кирилла спросим, что он думает"))
    time.sleep(1)
