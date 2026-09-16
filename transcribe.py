"""
Real-time transcription of Teams (or any app) audio output with simple speaker labels.

Pipeline:
  WASAPI loopback capture -> resample to 16 kHz mono -> Silero VAD utterance segmentation
  -> faster-whisper (CPU, int8) -> ECAPA speaker embedding -> online clustering (SPEAKER_00, 01, ...)
  -> console + transcript file (+ full WAV recording + JSONL segments)

Usage:
  python transcribe.py --list-devices
  python transcribe.py                       # default output device loopback, model large-v3-turbo, ru
  python transcribe.py --device "Sonar - Chat" --language auto --model medium
"""

import argparse
import datetime as dt
import json
import os
import queue
import sys
import threading
import time
import wave

import warnings

import numpy as np

from paths import BASE_DIR

warnings.filterwarnings("ignore")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

# ----------------------------------------------------------------------------- args

def parse_args():
    ap = argparse.ArgumentParser(description="Real-time loopback transcription with speaker labels")
    ap.add_argument("--list-devices", action="store_true", help="list WASAPI loopback devices and exit")
    ap.add_argument("--device", default=None, help="substring of loopback device name (default: system default output)")
    ap.add_argument("--file", default=None, help="process a WAV file instead of live audio (any rate, mono/stereo, 16-bit)")
    ap.add_argument("--process", default=None, metavar="EXE",
                    help="capture only this process's audio (e.g. ms-teams.exe) via WASAPI process loopback; needs proclb helper")
    ap.add_argument("--model", default="large-v3-turbo", help="faster-whisper model (large-v3-turbo, large-v3, medium, small)")
    ap.add_argument("--language", default="ru", help="language code or 'auto'")
    ap.add_argument("--compute-type", default="int8", help="ctranslate2 compute type (int8, int8_float32, float32)")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 8) // 2), help="CPU threads for whisper")
    ap.add_argument("--beam", type=int, default=5, help="beam size")
    ap.add_argument("--out", default="recordings", help="output directory")
    ap.add_argument("--no-diar", action="store_true", help="disable speaker labelling")
    ap.add_argument("--spk-threshold", type=float, default=0.45, help="cosine similarity to join an existing speaker")
    ap.add_argument("--min-silence", type=int, default=700, help="ms of silence that ends an utterance")
    ap.add_argument("--max-utt", type=float, default=25.0, help="force cut utterances longer than this (s)")
    ap.add_argument("--no-save-audio", action="store_true", help="do not write the full WAV recording")
    ap.add_argument("--initial-prompt", default=None, help="optional whisper prompt (names, terms)")
    ap.add_argument("--vosk-model", default=None, help="path to a Vosk model for the instant draft (default: models/vosk-model-small-ru-0.22)")
    ap.add_argument("--no-vosk", action="store_true", help="disable the instant Vosk draft")
    ap.add_argument("--proc-url", default="", help="external processor URL (processor_server.py), e.g. http://host:8756")
    ap.add_argument("--proc-token", default="", help="shared secret for the external processor")
    ap.add_argument("--proc-mode", default="local", choices=["local", "external"], help="where Whisper runs")
    ap.add_argument("--proc-model", default="", help="model to ask the external processor for (empty = server default)")
    ap.add_argument("--journal", action="store_true",
                    help="name the output after the current lesson from journal.top-academy.ru (needs journal.json)")
    return ap.parse_args()


# ----------------------------------------------------------------------------- audio capture

SR = 16000  # internal sample rate
PA_LOCK = threading.Lock()  # PortAudio/WASAPI device enumeration and stream opening are not thread-safe: serialize them


def _lowpass_fir(cutoff, fs, taps=97):
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.hamming(taps)
    return (h / h.sum()).astype(np.float32)


class Decimator:
    """Integer-ratio downsampler (48k/96k -> 16k) with a windowed-sinc anti-alias filter, streaming-safe."""

    def __init__(self, in_sr, out_sr):
        assert in_sr % out_sr == 0, f"unsupported rate {in_sr}"
        self.ratio = in_sr // out_sr
        self.h = _lowpass_fir(out_sr * 0.45, in_sr, taps=64 * self.ratio + 1)
        self.tail = np.zeros(len(self.h) - 1, dtype=np.float32)
        self.phase = 0

    def __call__(self, x):
        if self.ratio == 1:
            return x
        buf = np.concatenate([self.tail, x])
        y = np.convolve(buf, self.h, mode="valid")
        self.tail = buf[-(len(self.h) - 1):]
        out = y[self.phase::self.ratio]
        self.phase = (self.phase - len(y)) % self.ratio
        return out.astype(np.float32)


def list_loopback_devices():
    import pyaudiowpatch as pyaudio
    p = pyaudio.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_idx = wasapi["defaultOutputDevice"]
        print("Loopback devices (WASAPI):")
        for d in p.get_loopback_device_info_generator():
            mark = "  <- default output" if d["name"].startswith(p.get_device_info_by_index(default_idx)["name"]) else ""
            print(f"  [{d['index']:>2}] {d['name']}  ({int(d['defaultSampleRate'])} Hz, {d['maxInputChannels']} ch){mark}")
    finally:
        p.terminate()


def list_input_devices():
    """WASAPI capture endpoints: loopbacks of every output device plus real inputs (mics, virtual cables)."""
    import pyaudiowpatch as pyaudio
    with PA_LOCK:
        return _list_input_devices(pyaudio)


def _list_input_devices(pyaudio):
    p = pyaudio.PyAudio()
    out = []
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"] or d["maxInputChannels"] <= 0:
                continue
            out.append({"index": i, "name": d["name"], "rate": int(d["defaultSampleRate"]),
                        "channels": int(d["maxInputChannels"]), "loopback": bool(d.get("isLoopbackDevice")),
                        "default": i == wasapi["defaultInputDevice"]})
    finally:
        p.terminate()
    out.sort(key=lambda d: (not d["loopback"], d["name"].lower()))
    return out


def list_output_devices():
    import pyaudiowpatch as pyaudio
    with PA_LOCK:
        return _list_output_devices(pyaudio)


def _list_output_devices(pyaudio):
    p = pyaudio.PyAudio()
    out = []
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"] or d["maxOutputChannels"] <= 0 or d.get("isLoopbackDevice"):
                continue
            out.append({"index": i, "name": d["name"], "rate": int(d["defaultSampleRate"]),
                        "channels": int(d["maxOutputChannels"]), "default": i == wasapi["defaultOutputDevice"]})
    finally:
        p.terminate()
    out.sort(key=lambda d: d["name"].lower())
    return out


class Monitor(threading.Thread):
    """Plays captured audio to another output device (the 'virtual layer' output), with volume."""

    def __init__(self, p, out_index, in_rate, volume=1.0):
        super().__init__(daemon=True)
        import pyaudiowpatch as pyaudio
        d = p.get_device_info_by_index(out_index)
        self.name = d["name"]
        self.rate = int(d["defaultSampleRate"])
        self.in_rate = in_rate
        self.volume = volume
        self.q = queue.Queue(maxsize=20)
        # WASAPI shared mode wants the endpoint's own channel count (Sonar devices are 8-ch): try it first, then stereo/mono
        last = None
        for ch in dict.fromkeys([int(d["maxOutputChannels"]), 2, 1]):
            try:
                self.stream = p.open(format=pyaudio.paFloat32, channels=ch, rate=self.rate, output=True,
                                     output_device_index=out_index, frames_per_buffer=int(self.rate * 0.05))
                self.channels = ch
                break
            except Exception as e:
                last = e
        else:
            raise RuntimeError(f"cannot open output '{self.name}': {last}")
        self.stop_event = threading.Event()

    def push(self, stereo):
        try:
            self.q.put_nowait(stereo)
        except queue.Full:
            pass

    def run(self):
        while not self.stop_event.is_set():
            try:
                x = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.rate != self.in_rate:
                n_out = int(len(x) * self.rate / self.in_rate)
                t_in = np.arange(len(x)) * (self.rate / self.in_rate)
                t_out = np.arange(n_out)
                x = np.stack([np.interp(t_out, t_in, x[:, c]) for c in range(x.shape[1])], axis=1)
            if x.shape[1] != self.channels:
                if x.shape[1] == 1:
                    x = np.repeat(x, min(2, self.channels), axis=1)
                if x.shape[1] < self.channels:   # stereo into front L/R, other channels silent
                    x = np.concatenate([x, np.zeros((len(x), self.channels - x.shape[1]), dtype=np.float32)], axis=1)
                else:
                    x = x[:, :self.channels]
            y = np.clip(x * self.volume, -1, 1).astype(np.float32)
            try:
                self.stream.write(np.ascontiguousarray(y).tobytes())
            except Exception:
                break
        try:
            self.stream.stop_stream(); self.stream.close()
        except Exception:
            pass


def pick_loopback_device(p, substring):
    import pyaudiowpatch as pyaudio
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    if substring:
        for d in p.get_loopback_device_info_generator():
            if substring.lower() in d["name"].lower():
                return d
        raise SystemExit(f"No loopback device matching '{substring}'. Use --list-devices.")
    default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if default_out.get("isLoopbackDevice"):
        return default_out
    for d in p.get_loopback_device_info_generator():
        if d["name"].startswith(default_out["name"]):
            return d
    raise SystemExit("Default output device has no loopback counterpart. Use --list-devices / --device.")


class Fanout:
    """Forwards put() to several queues (live audio feeds both the VAD/Whisper path and the Vosk draft)."""

    def __init__(self, *qs):
        self.qs = qs

    def put(self, x):
        for q in self.qs:
            q.put(x)

    def qsize(self):
        return max(q.qsize() for q in self.qs)


class Console:
    """Final lines scroll normally; the live draft is redrawn on the bottom line."""

    DIM, RESET = "\x1b[2m", "\x1b[0m"

    def __init__(self):
        self.lock = threading.Lock()
        self.draft = ""
        self.sink = None   # object with final(line, dim) / partial(text) / draft_done(text): GUI hook
        try:
            self.tty = sys.stdout.isatty()
        except Exception:
            self.tty = False
        if os.name == "nt":
            try:  # enable ANSI escape processing in the classic console
                import ctypes
                k = ctypes.windll.kernel32
                h = k.GetStdHandle(-11)
                mode = ctypes.c_uint32()
                k.GetConsoleMode(h, ctypes.byref(mode))
                k.SetConsoleMode(h, mode.value | 0x0004)
            except Exception:
                pass

    def _clear(self):
        if self.tty:
            sys.stdout.write("\r\x1b[K")

    def _draw(self):
        if self.tty and self.draft:
            import shutil
            w = max(20, shutil.get_terminal_size().columns - 4)
            sys.stdout.write(self.DIM + "~ " + self.draft[-w:] + self.RESET)
        sys.stdout.flush()

    def emit(self, kind, **payload):
        """Structured event for GUI sinks; returns False when no sink handles events (CLI falls back to text)."""
        if self.sink is not None and hasattr(self.sink, "event"):
            self.sink.event(kind, payload)
            return True
        return False

    def final(self, line, dim=False):
        if self.sink is not None:
            self.sink.final(line, dim); return
        with self.lock:
            self._clear()
            sys.stdout.write((self.DIM + line + self.RESET if dim and self.tty else line) + "\n")
            self._draw()

    def partial(self, text):
        if self.sink is not None:
            self.sink.partial(text); return
        with self.lock:
            self.draft = text
            self._clear()
            self._draw()

    def draft_done(self, text):
        if self.sink is not None:
            self.sink.draft_done(text); return
        with self.lock:
            self.draft = ""
            self._clear()
            if text:
                sys.stdout.write((self.DIM + "~ " + text + self.RESET if self.tty else "~ " + text) + "\n")
            sys.stdout.flush()


CON = Console()


class Drafter(threading.Thread):
    """Instant streaming draft with Vosk (Kaldi). Console only, never written to the transcript file."""

    def __init__(self, model_path, in_queue):
        super().__init__(daemon=True)
        self.model_path = model_path
        self.in_q = in_queue

    def run(self):
        import vosk
        vosk.SetLogLevel(-1)
        model = vosk.Model(self.model_path)
        rec = vosk.KaldiRecognizer(model, SR)
        rec.SetWords(True)
        last_partial = ""
        stream_pos = 0          # samples fed to the recognizer (its timestamps are relative to this stream)
        offset = time.time()    # wall time of stream sample 0, re-estimated on every chunk (absorbs drift/gaps)
        while True:
            item = self.in_q.get()
            if item is None:
                break
            wall, x = item
            stream_pos += len(x)
            offset = wall - stream_pos / SR
            data = (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()
            if rec.AcceptWaveform(data):
                res = json.loads(rec.Result())
                text = res.get("text", "").strip()
                last_partial = ""
                if not text:
                    continue
                words = [{"w": w["word"], "t0": offset + w["start"], "t1": offset + w["end"]} for w in res.get("result", [])]
                if not CON.emit("draft", text=text, words=words,
                                t0=words[0]["t0"] if words else wall, t1=words[-1]["t1"] if words else wall):
                    CON.draft_done(text)
            else:
                text = json.loads(rec.PartialResult()).get("partial", "").strip()
                if text and text != last_partial:
                    if not CON.emit("partial", text=text, t=wall):
                        CON.partial(text)
                    last_partial = text


class Recorder(threading.Thread):
    """Captures loopback audio, downmixes to mono 16 kHz float32, pushes chunks to a queue."""

    def __init__(self, device_substring, out_queue, wav_path=None, monitor_index=None, monitor_volume=1.0):
        super().__init__(daemon=True)
        self.device_substring = device_substring   # substring, or an int device index
        self.q = out_queue
        self.wav_path = wav_path
        self.stop_event = threading.Event()
        self.device_name = None
        self.monitor_index = monitor_index
        self.monitor_volume = monitor_volume
        self.monitor = None
        self.level = 0.0   # last chunk peak (0..1) for UI meters
        self.error = None

    def run(self):
        import pyaudiowpatch as pyaudio

        PA_LOCK.acquire()
        locked = True
        p = pyaudio.PyAudio()
        try:
            if isinstance(self.device_substring, int):
                dev = p.get_device_info_by_index(self.device_substring)
            else:
                dev = pick_loopback_device(p, self.device_substring)
        except Exception as e:
            self.error = str(e); self.q.put(None); p.terminate(); PA_LOCK.release(); return
        self.device_name = dev["name"]
        in_sr = int(dev["defaultSampleRate"])
        channels = int(dev["maxInputChannels"])
        frames_per_buffer = int(in_sr * 0.1)  # 100 ms

        wav = None
        if self.wav_path:
            wav = wave.open(self.wav_path, "wb")
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SR)

        try:
            stream = p.open(format=pyaudio.paFloat32, channels=channels, rate=in_sr, input=True,
                            input_device_index=dev["index"], frames_per_buffer=frames_per_buffer)
        except Exception as e:
            self.error = f"cannot open input '{dev['name']}': {e}"; self.q.put(None); p.terminate(); PA_LOCK.release(); return
        if self.monitor_index is not None:
            try:
                self.monitor = Monitor(p, self.monitor_index, in_sr, self.monitor_volume)
                self.monitor.start()
            except Exception as e:
                CON.final(f"[error] выход не открылся, звук дальше не пересылается: {e}")
                self.monitor = None
        PA_LOCK.release()
        locked = False
        chunk_dur = frames_per_buffer / in_sr
        expected = None
        decim = Decimator(in_sr, SR)
        run_peak = 0.0  # slow-decaying peak tracker for automatic gain
        try:
            while not self.stop_event.is_set():
                data = stream.read(frames_per_buffer, exception_on_overflow=False)
                now = time.time()
                # WASAPI loopback delivers nothing while no app renders audio: pad the gap with silence
                if expected is not None and now - expected > 0.5:
                    gap = min(now - expected, 3.0)
                    z = np.zeros(int(gap * SR), dtype=np.float32)
                    if wav is not None:
                        wav.writeframes(np.zeros(int((now - expected) * SR), dtype=np.int16).tobytes())
                    self.q.put((now - gap, z))
                expected = now + chunk_dur
                x = np.frombuffer(data, dtype=np.float32)
                if channels > 1:
                    st = x.reshape(-1, channels)[:, :2]
                    if self.monitor is not None:
                        self.monitor.push(st)
                    x = st.mean(axis=1)  # L+R only (surround channels are usually silent)
                elif self.monitor is not None:
                    self.monitor.push(x.reshape(-1, 1))
                x = decim(np.ascontiguousarray(x, dtype=np.float32))
                pk = float(np.abs(x).max()) if len(x) else 0.0
                self.level = pk
                run_peak = max(pk, run_peak * 0.997)          # ~30 s to decay by half
                if run_peak > 0.003:                          # ignore idle noise floor
                    gain = min(60.0, 0.6 / run_peak)          # up to +35 dB, never attenuate
                    if gain > 1.0:
                        x = x * gain
                if wav is not None:
                    wav.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
                self.q.put((now, x))
        finally:
            if locked:
                PA_LOCK.release()
            if self.monitor is not None:
                self.monitor.stop_event.set()
                self.monitor.join(timeout=2)
            with PA_LOCK:
                stream.stop_stream()
                stream.close()
                p.terminate()
            if wav is not None:
                wav.close()
            self.q.put(None)


class ProcessRecorder(threading.Thread):
    """Captures the audio rendered by one process (and its children) through the proclb helper."""

    HELPER = os.path.join(BASE_DIR, "proclb", "bin", "Release", "net9.0-windows", "proclb.exe")
    RATE = 48000

    def __init__(self, exe, out_queue, wav_path=None):
        super().__init__(daemon=True)
        self.exe = exe
        self.q = out_queue
        self.wav_path = wav_path
        self.stop_event = threading.Event()
        self.device_name = None
        self.error = None

    def run(self):
        import subprocess
        if not os.path.exists(self.HELPER):
            self.error = f"helper not found: {self.HELPER} (build: cd proclb && dotnet build -c Release)"
            self.q.put(None); return
        proc = subprocess.Popen([self.HELPER, "--name", self.exe, "--rate", str(self.RATE)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def drain_stderr():
            for line in proc.stderr:
                msg = line.decode("utf-8", "replace").rstrip()
                if msg.startswith("proclb: capturing"):
                    self.device_name = f"process {self.exe} (WASAPI process loopback)"
                elif msg:
                    self.error = msg
                    print(f"[audio] {msg}", flush=True)
        threading.Thread(target=drain_stderr, daemon=True).start()

        wav = None
        if self.wav_path:
            wav = wave.open(self.wav_path, "wb"); wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(SR)
        decim = Decimator(self.RATE, SR)
        block = int(self.RATE * 0.1) * 4  # 100 ms of int16 stereo
        expected = None
        run_peak = 0.0
        try:
            while not self.stop_event.is_set():
                data = proc.stdout.read(block)
                if not data:
                    break
                now = time.time()
                if expected is not None and now - expected > 0.5:
                    gap = min(now - expected, 3.0)
                    if wav is not None:
                        wav.writeframes(np.zeros(int((now - expected) * SR), dtype=np.int16).tobytes())
                    self.q.put((now - gap, np.zeros(int(gap * SR), dtype=np.float32)))
                expected = now + len(data) / 4 / self.RATE
                x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                x = x.reshape(-1, 2).mean(axis=1)
                x = decim(np.ascontiguousarray(x, dtype=np.float32))
                pk = float(np.abs(x).max()) if len(x) else 0.0
                run_peak = max(pk, run_peak * 0.997)
                if run_peak > 0.003:
                    gain = min(60.0, 0.6 / run_peak)
                    if gain > 1.0:
                        x = x * gain
                if wav is not None:
                    wav.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
                self.q.put((now, x))
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            if wav is not None:
                wav.close()
            self.q.put(None)


class FileFeeder(threading.Thread):
    """Feeds a WAV file through the same pipeline as live audio (as fast as the consumer takes it)."""

    def __init__(self, path, out_queue):
        super().__init__(daemon=True)
        self.path = path
        self.q = out_queue
        self.stop_event = threading.Event()
        self.device_name = f"file: {path}"

    def run(self):
        w = wave.open(self.path, "rb")
        ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        assert sw == 2, "16-bit PCM WAV expected"
        decim = Decimator(sr, SR) if sr % SR == 0 else None
        step = sr // 10
        pos = 0.0
        while not self.stop_event.is_set():
            raw = w.readframes(step)
            if not raw:
                break
            x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
            if ch > 1:
                x = x.reshape(-1, ch)[:, :2].mean(axis=1)
            if decim is not None:
                x = decim(np.ascontiguousarray(x, dtype=np.float32))
            elif sr != SR:
                x = np.interp(np.arange(0, len(x), sr / SR), np.arange(len(x)), x).astype(np.float32)
            self.q.put((pos, x))
            pos += step / sr
            while self.q.qsize() > 50 and not self.stop_event.is_set():
                time.sleep(0.05)
        self.q.put(None)


# ----------------------------------------------------------------------------- VAD segmentation

class Segmenter(threading.Thread):
    """Turns the continuous audio stream into utterances using Silero VAD."""

    FRAME = 512  # samples @16k = 32 ms, required by silero

    def __init__(self, in_queue, out_queue, min_silence_ms, max_utt_s, tag="teams", gate=None):
        super().__init__(daemon=True)
        self.in_q = in_queue
        self.out_q = out_queue
        self.min_silence_ms = min_silence_ms
        self.max_utt = max_utt_s
        self.pad = int(0.25 * SR)
        self.tag = tag        # "teams" (other people) or "mic" (you)
        self.gate = gate      # callable -> bool; False = treat incoming audio as silence (mic muted in Teams)

    def run(self):
        import torch
        from silero_vad import load_silero_vad, VADIterator

        model = load_silero_vad()
        vad = VADIterator(model, threshold=0.5, sampling_rate=SR,
                          min_silence_duration_ms=self.min_silence_ms, speech_pad_ms=100)

        pending = np.zeros(0, dtype=np.float32)   # not yet fed into VAD
        history = np.zeros(0, dtype=np.float32)   # everything since utterance start (or ring for padding)
        utt_start = None                           # absolute sample index
        utt_wall = None                            # wall-clock time of utterance start
        chunk_wall = time.time()                   # wall-clock time of the chunk being fed

        def flush(end_abs):
            nonlocal history, utt_start
            if utt_start is None:
                return
            s = max(0, utt_start - self.pad)
            hist_start_abs = total_fed - len(history)
            a = s - hist_start_abs
            b = end_abs - hist_start_abs
            seg = history[max(0, a):max(0, b)]
            if len(seg) > int(0.5 * SR):
                self.out_q.put((utt_wall, seg.copy(), self.tag))
            utt_start = None

        total_fed = 0
        while True:
            chunk = self.in_q.get()
            if chunk is None:
                if utt_start is not None:
                    flush(total_fed)
                self.out_q.put(None)
                return
            chunk_wall, chunk = chunk
            if self.gate is not None and not self.gate():
                chunk = np.zeros_like(chunk)  # gated off: silence, so an open utterance closes normally
            pending = np.concatenate([pending, chunk])
            while len(pending) >= self.FRAME:
                frame = pending[:self.FRAME]
                pending = pending[self.FRAME:]
                history = np.concatenate([history, frame])
                total_fed += self.FRAME
                ev = vad(torch.from_numpy(frame), return_seconds=False)
                if ev is not None:
                    if "start" in ev and utt_start is None:
                        utt_start = int(ev["start"])
                        utt_wall = chunk_wall - (total_fed - utt_start) / SR
                    elif "end" in ev and utt_start is not None:
                        flush(int(ev["end"]))
                # force-cut long utterances so latency stays bounded
                if utt_start is not None and (total_fed - utt_start) / SR >= self.max_utt:
                    flush(total_fed)
                    utt_start = total_fed  # continue as new utterance without re-detecting
                    utt_wall = chunk_wall
                # keep history bounded when idle
                if utt_start is None and len(history) > self.pad * 4:
                    history = history[-self.pad * 2:]


# ----------------------------------------------------------------------------- speaker labelling

class SpeakerTracker:
    def __init__(self, threshold):
        import torch
        from speechbrain.inference.speaker import EncoderClassifier
        from speechbrain.utils.fetching import LocalStrategy
        cache = os.path.join(BASE_DIR, "models", "ecapa")
        self.enc = EncoderClassifier.from_hparams(source="speechbrain/spkrec-ecapa-voxceleb", savedir=cache,
                                                  run_opts={"device": "cpu"},
                                                  local_strategy=LocalStrategy.COPY)
        self.threshold = threshold
        self.centroids = []   # normalized mean embeddings
        self.counts = []
        self.last = None

    def embed(self, audio):
        import torch
        with torch.no_grad():
            e = self.enc.encode_batch(torch.from_numpy(audio)[None]).squeeze().numpy()
        return e / (np.linalg.norm(e) + 1e-9)

    def label(self, audio):
        short = len(audio) < int(2.0 * SR)
        if len(audio) < int(0.8 * SR):
            return self.last if self.last is not None else "SPEAKER_00"
        e = self.embed(audio)
        if not self.centroids:
            self.centroids.append(e); self.counts.append(1)
            self.last = "SPEAKER_00"
            return self.last
        sims = [float(np.dot(e, c)) for c in self.centroids]
        i = int(np.argmax(sims))
        if short and sims[i] < self.threshold:
            # too little audio to open a new cluster: lean towards the best match / previous speaker
            if sims[i] >= self.threshold - 0.12:
                self.last = f"SPEAKER_{i:02d}"
            return self.last
        if sims[i] >= self.threshold:
            n = self.counts[i]
            c = self.centroids[i] * n + e
            self.centroids[i] = c / (np.linalg.norm(c) + 1e-9)
            self.counts[i] = min(n + 1, 50)
        else:
            self.centroids.append(e); self.counts.append(1)
            i = len(self.centroids) - 1
        self.last = f"SPEAKER_{i:02d}"
        return self.last


# ----------------------------------------------------------------------------- remote processor (optional)

class _Seg:
    __slots__ = ("start", "end", "text", "no_speech_prob")

    def __init__(self, d):
        self.start = float(d.get("start", 0.0)); self.end = float(d.get("end", 0.0))
        self.text = d.get("text", ""); self.no_speech_prob = float(d.get("no_speech_prob", 0.0))


class _Info:
    __slots__ = ("language",)

    def __init__(self, lang):
        self.language = lang


class RemoteWhisper:
    """Same call shape as faster_whisper.WhisperModel.transcribe, but the decode happens on processor_server.py.
    Falls back to a locally loaded model when the server is unreachable, so a lesson is never lost."""

    def __init__(self, url, token="", timeout=120, local_factory=None, on_log=None, model=None):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self.local_factory = local_factory     # callable -> WhisperModel, used only if the server fails
        self.model = model or ""               # ask the server for this model ("" = server default)
        self.on_log = on_log or (lambda m: None)
        self._local = None
        self._failures = 0
        self._warned = False
        self._remote_ok = True                 # False after a failed request; a background probe flips it back
        self._probe_thread = None
        self.probe_interval = 20               # seconds between health probes while the server is down

    def health(self):
        import urllib.request
        req = urllib.request.Request(self.url + "/health", headers={"X-Token": self.token})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def _fallback(self, audio, kw, why):
        if self.local_factory is None:
            raise RuntimeError(why)
        if self._remote_ok:
            self._remote_ok = False
            self.on_log(f"[proc] внешний обработчик недоступен ({why}); переключаюсь на локальный, "
                        f"проверяю сервер каждые {self.probe_interval} с")
            self._start_probe()
        if self._local is None:
            self._local = self.local_factory()
        return self._local.transcribe(audio, **kw)

    def _start_probe(self):
        """Background thread: poll /health until the server answers, then route requests back to it."""
        if self._probe_thread is not None and self._probe_thread.is_alive():
            return
        def run():
            while not self._remote_ok:
                time.sleep(self.probe_interval)
                if self._remote_ok:
                    break
                try:
                    h = self.health()
                except Exception:
                    continue
                if h.get("ok"):
                    self._remote_ok = True
                    self.on_log(f"[proc] внешний обработчик снова доступен ({h.get('device')}, "
                                f"{h.get('model')}); возвращаюсь на сервер")
        self._probe_thread = threading.Thread(target=run, daemon=True, name="proc-probe")
        self._probe_thread.start()

    def transcribe(self, audio, language=None, beam_size=5, vad_filter=False,
                   condition_on_previous_text=False, initial_prompt=None, no_speech_threshold=0.6):
        import base64, urllib.error, urllib.request
        kw = dict(language=language, beam_size=beam_size, vad_filter=vad_filter,
                  condition_on_previous_text=condition_on_previous_text,
                  initial_prompt=initial_prompt, no_speech_threshold=no_speech_threshold)
        if not self._remote_ok:              # server is down; the probe thread will bring it back
            return self._local.transcribe(audio, **kw)
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        body = json.dumps({"sr": SR, "pcm16": base64.b64encode(pcm).decode("ascii"), "language": language,
                           "beam": beam_size, "model": self.model or None, "initial_prompt": initial_prompt,
                           "no_speech_threshold": no_speech_threshold}).encode("utf-8")
        req = urllib.request.Request(self.url + "/transcribe", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Token": self.token})
        data = None
        last_err = None
        for attempt in range(2):             # a second try covers a sub-second network blip
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except Exception as e:
                last_err = e
                self._failures += 1
                if attempt == 0 and time.time() - t0 < 15:   # failed fast (refused/reset/DNS) -> retry once
                    time.sleep(1.5)
                    continue
                break
        if data is None:
            return self._fallback(audio, kw, str(last_err)[:120])
        return [_Seg(d) for d in data.get("segments", [])], _Info(data.get("language"))


# ----------------------------------------------------------------------------- ASR worker

HALLUCINATIONS = (
    "субтитры", "продолжение следует", "редактор субтитров", "спасибо за просмотр",
    "subtitles by", "thanks for watching", "thank you for watching", "thank you.", "thank you", "bye.",
)


def fmt_ts(wall):
    if wall < 1e6:  # file mode: offset inside the file
        return str(dt.timedelta(seconds=int(wall)))
    return dt.datetime.fromtimestamp(wall).strftime("%H:%M:%S")


class Transcriber(threading.Thread):
    def header(self):
        """Technical info written once at the top of a new transcript file."""
        a = self.args
        meta = getattr(a, "meta", None) or {}
        started = getattr(self, "started", None) or dt.datetime.now()
        lines = []
        if meta.get("time"):
            lines.append(f"# Пара: {meta['subject']}")
            if meta.get("teacher"):
                lines.append(f"# Преподаватель: {meta['teacher']}")
            lines.append(f"# Дата: {meta['date']}, {meta['time']}")
        elif meta:
            lines.append(f"# {meta['subject']} ({meta['date']}, пар в журнале: {meta['lessons_today']})")
        lines.append(f"# Запись начата: {started:%Y-%m-%d %H:%M:%S}")
        src = getattr(getattr(self, "rec", None), "device_name", None) or a.file or a.process or a.device or "default output"
        lines.append(f"# Источник: {src}")
        if getattr(self, "mic_name", None):
            lines.append(f"# Микрофон (реплики «Вы»): {self.mic_name}")
        vosk = getattr(self, "vosk_path", None)
        if getattr(a, "proc_mode", "local") == "external" and getattr(a, "proc_url", ""):
            lines.append(f"# Обработчик: внешний {a.proc_url}" + (f", модель {a.proc_model}" if getattr(a, "proc_model", "") else ""))
        lines.append(f"# Модель: whisper {a.model} ({a.compute_type}), язык {a.language}"
                     + (f", черновик {os.path.basename(vosk)}" if vosk else "")
                     + ("" if a.no_diar else ", спикеры ECAPA"))
        wav = getattr(self, "wav_path", None)
        lines.append(f"# Файлы: {os.path.basename(self.jsonl_path)}" + (f", {os.path.basename(wav)}" if wav else ""))
        lines.append("# ---")
        return "\n".join(lines) + "\n"

    def __init__(self, args, in_queue, out_dir, session, n_sources=1):
        super().__init__(daemon=True)
        self.args = args
        self.in_q = in_queue
        self.n_sources = n_sources  # how many segmenters feed in_q (each sends one None at the end)
        self.txt_path = os.path.join(out_dir, f"{session}.txt")
        self.jsonl_path = os.path.join(out_dir, f"{session}.jsonl")
        self.done = threading.Event()
        self.t_session = 0.0 if args.file else time.time()

    def run(self):
        from faster_whisper import WhisperModel
        a = self.args
        local = lambda: WhisperModel(a.model, device="cpu", compute_type=a.compute_type, cpu_threads=a.threads)
        url = getattr(a, "proc_url", "") if getattr(a, "proc_mode", "local") == "external" else ""
        if url:
            CON.final(f"[init] внешний обработчик: {url}")
            model = RemoteWhisper(url, getattr(a, "proc_token", ""), local_factory=local, on_log=CON.final,
                                  model=getattr(a, "proc_model", ""))
            try:
                h = model.health()
                want = getattr(a, "proc_model", "") or h.get("model")
                CON.final(f"[proc] сервер отвечает: whisper {want} на {h.get('device')}"
                          + ("" if h.get("allow_model_switch", True) else " (модель задана на сервере)"))
            except Exception as e:
                CON.final(f"[proc] сервер не отвечает ({str(e)[:80]}), загружаю локальную модель")
                print(f"[init] loading whisper '{a.model}' ({a.compute_type}, {a.threads} threads) ...", flush=True)
                model = local()
        else:
            print(f"[init] loading whisper '{a.model}' ({a.compute_type}, {a.threads} threads) ...", flush=True)
            model = local()
        spk = None
        if not a.no_diar:
            print("[init] loading speaker encoder ...", flush=True)
            spk = SpeakerTracker(a.spk_threshold)
        lang = None if a.language.lower() == "auto" else a.language
        CON.final("[ready] listening. Ctrl+C to stop.\n")

        GAP = int(0.5 * SR)
        MAX_WIN = 28.0
        fresh = not os.path.exists(self.txt_path) or os.path.getsize(self.txt_path) == 0
        with open(self.txt_path, "a", encoding="utf-8") as ftxt, open(self.jsonl_path, "a", encoding="utf-8") as fjs:
            ftxt.write(self.header() if fresh else f"# --- продолжение записи {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n")
            ftxt.flush()
            finished = False
            sentinels = 0
            while not finished:
                item = self.in_q.get()
                if item is None:
                    sentinels += 1
                    if sentinels >= self.n_sources:
                        break
                    continue
                # gather everything already waiting (the encoder cost is ~constant per 30 s window)
                batch = [item]
                total = len(item[1]) / SR
                while True:
                    try:
                        nxt = self.in_q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        sentinels += 1
                        if sentinels >= self.n_sources:
                            finished = True
                            break
                        continue
                    if total + len(nxt[1]) / SR + 0.5 > MAX_WIN:
                        self.in_q.queue.appendleft(nxt)  # put it back to the front
                        break
                    batch.append(nxt)
                    total += len(nxt[1]) / SR + 0.5

                t0 = time.time()
                offsets, parts = [], []
                pos = 0
                for _, au, _src in batch:
                    offsets.append((pos / SR, (pos + len(au)) / SR))
                    parts.append(au); parts.append(np.zeros(GAP, dtype=np.float32))
                    pos += len(au) + GAP
                audio = np.concatenate(parts)

                # adaptive decoding: when the queue grows (CPU busy with something else) trade beam width for speed
                backlog_now = self.in_q.qsize()
                beam = a.beam if backlog_now <= 1 else (max(2, a.beam - 2) if backlog_now <= 4 else 1)
                segments, info = model.transcribe(
                    audio, language=lang, beam_size=beam, vad_filter=False,
                    condition_on_previous_text=False, initial_prompt=a.initial_prompt,
                    no_speech_threshold=0.6,
                )
                texts = [[] for _ in batch]
                for sgm in segments:
                    if sgm.no_speech_prob > 0.75:
                        continue
                    mid = (sgm.start + sgm.end) / 2
                    k = min(range(len(offsets)), key=lambda j: 0 if offsets[j][0] <= mid < offsets[j][1] + 0.5
                            else min(abs(mid - offsets[j][0]), abs(mid - offsets[j][1])))
                    texts[k].append(sgm.text.strip())
                proc = time.time() - t0

                for (start_s, au, src), tx in zip(batch, texts):
                    text = " ".join(t for t in tx if t).strip()
                    if not text or (any(h in text.lower() for h in HALLUCINATIONS) and len(text) < 40):
                        continue
                    if src == "mic":
                        label = "Вы"
                    else:
                        label = spk.label(au) if spk else "SPEAKER"
                    dur = len(au) / SR
                    line = f"[{fmt_ts(start_s)}] {label}: {text}"
                    if not CON.emit("final", t0=start_s, t1=start_s + dur, speaker=label, text=text, wall=fmt_ts(start_s), source=src):
                        CON.final(line)
                    who = "Вы" if src == "mic" else label.replace("SPEAKER_", "Спикер ")
                    ftxt.write(f"{who}: {text}\n"); ftxt.flush()
                    fjs.write(json.dumps({"start": round(start_s - self.t_session, 2), "end": round(start_s - self.t_session + dur, 2),
                                          "wall": fmt_ts(start_s), "ts": round(start_s, 2), "speaker": label, "source": src,
                                          "text": text, "lang": info.language}, ensure_ascii=False) + "\n"); fjs.flush()
                backlog = self.in_q.qsize()
                if not CON.emit("stats", utts=len(batch), audio=round(total, 1), proc=round(proc, 1), backlog=backlog, beam=beam):
                    CON.final(f"    . {len(batch)} utt / {total:.0f}s audio in {proc:.1f}s" + (f", backlog {backlog}" if backlog else ""), dim=True)
        self.done.set()


# ----------------------------------------------------------------------------- main

class Pipeline:
    """Everything main() used to do, as an object the GUI can start/stop. `args` is an argparse-like namespace."""

    def __init__(self, args):
        self.args = args
        self.rec = self.seg = self.asr = self.drafter = None
        self.session = None
        self.txt_path = None
        self.info = ""

    def start(self):
        args = self.args
        import torch
        torch.set_num_threads(2)  # VAD + speaker encoder are light; leave the cores to ctranslate2
        now = dt.datetime.now()
        # layout: <out>/<subject>/<date_start>/transcript.{txt,jsonl} + audio__HH-MM-SS.wav (+ mic__HH-MM-SS.wav)
        out_dir = os.path.join(args.out, "без-пар", now.strftime("%Y-%m-%d_%H-%M"))
        session = "transcript"
        wav_name = f"audio__{now:%H-%M-%S}"
        if args.journal and not args.file:
            try:
                import journal
                name, info, meta = journal.session_name(now)
                args.meta = meta
                self.info = info
                if meta.get("time"):
                    start = meta["time"].split("-")[0].replace(":", "-")
                    out_dir = os.path.join(args.out, journal._slug(meta["subject"], 60), f"{meta['date']}_{start}")
                else:
                    out_dir = os.path.join(args.out, meta.get("subject", "вне-пар"), now.strftime("%Y-%m-%d_%H-%M"))
                CON.final(f"[journal] {info}")
            except Exception as e:
                self.info = f"journal lookup failed: {e}"
                CON.final(f"[journal] lookup failed, using timestamp folder: {e}")
        elif args.file:
            out_dir = os.path.join(args.out, "файлы", os.path.splitext(os.path.basename(args.file))[0])
        os.makedirs(out_dir, exist_ok=True)
        args.out = out_dir
        wav_path = None if (args.no_save_audio or args.file) else os.path.join(out_dir, f"{wav_name}.wav")

        raw_q = queue.Queue()
        utt_q = queue.Queue()

        vosk_path = None
        if not args.no_vosk:
            here = os.path.join(BASE_DIR, "models")
            if args.vosk_model:
                vosk_path = args.vosk_model
            elif args.language.lower() == "ru":
                for name in ("vosk-model-small-ru-0.22",):  # the big ru-0.42 loads ~4 min and needs 6 GB: opt-in via --vosk-model
                    if os.path.isdir(os.path.join(here, name)):
                        vosk_path = os.path.join(here, name)
                        break
        src_q = raw_q
        if vosk_path:
            vosk_q = queue.Queue()
            self.drafter = Drafter(vosk_path, vosk_q)
            self.drafter.start()
            src_q = Fanout(raw_q, vosk_q)
            CON.final(f"[init] vosk draft: {os.path.basename(vosk_path)}")

        if args.file:
            rec = FileFeeder(args.file, src_q)
        elif args.process:
            rec = ProcessRecorder(args.process, src_q, wav_path)
        else:
            rec = Recorder(args.device, src_q, wav_path, getattr(args, "monitor", None), getattr(args, "monitor_volume", 1.0))
        seg = Segmenter(raw_q, utt_q, args.min_silence, args.max_utt)

        # --- your own voice: a second capture on the microphone, labelled "Вы", optionally gated by Teams' mic button
        mic_dev = getattr(args, "mic", None)
        mic_mode = getattr(args, "mic_mode", "off")  # off | always | teams
        self.mic_rec = self.mic_seg = self.watcher = None
        if mic_dev is not None and mic_mode != "off" and not args.file:
            gate = None
            if mic_mode == "teams":
                from teams_mic import TeamsMicWatcher

                def on_change(st):
                    if not CON.emit("teams_mic", **st):
                        CON.final("[teams] " + ("в звонке" if st["in_call"] else "не в звонке")
                                  + ", микрофон " + ("вкл" if st["mic_on"] else "выкл"))
                self.watcher = TeamsMicWatcher(on_change=on_change)
                self.watcher.start()
                gate = self.watcher.mic_on
            mic_q = queue.Queue()
            mic_wav = None if args.no_save_audio else os.path.join(args.out, f"mic__{now:%H-%M-%S}.wav")
            self.mic_rec = Recorder(mic_dev, mic_q, mic_wav)
            self.mic_seg = Segmenter(mic_q, utt_q, args.min_silence, args.max_utt, tag="mic", gate=gate)

        asr = Transcriber(args, utt_q, args.out, session, n_sources=2 if self.mic_seg else 1)
        asr.rec, asr.wav_path, asr.vosk_path, asr.started = rec, wav_path, vosk_path, now
        asr.mic_name = None
        self.rec, self.seg, self.asr, self.session, self.txt_path = rec, seg, asr, session, asr.txt_path

        asr.start()
        seg.start()
        rec.start()
        if self.mic_seg:
            self.mic_seg.start()
            self.mic_rec.start()
        for _ in range(40):  # helper/device start-up
            if rec.device_name or getattr(rec, "error", None):
                break
            time.sleep(0.1)
        if rec.device_name:
            CON.final(f"[audio] capturing: {rec.device_name}"
                      + (f" -> playing to: {rec.monitor.name}" if getattr(rec, "monitor", None) else ""))
        if self.mic_rec is not None:
            for _ in range(30):
                if self.mic_rec.device_name or self.mic_rec.error:
                    break
                time.sleep(0.1)
            asr.mic_name = self.mic_rec.device_name
            CON.final(f"[audio] microphone: {self.mic_rec.device_name or self.mic_rec.error} "
                      + ("(только когда микрофон в Teams включён)" if mic_mode == "teams" else "(всегда)"))
        elif getattr(rec, "error", None):
            CON.final(f"[error] {rec.error}")
        CON.final(f"[out] {asr.txt_path}")
        return self

    def alive(self):
        return self.asr is not None and self.asr.is_alive()

    def capture_alive(self):
        return self.rec is not None and self.rec.is_alive()

    def stop(self, timeout=120):
        if self.rec is not None:
            self.rec.stop_event.set()
        if getattr(self, "mic_rec", None) is not None:
            self.mic_rec.stop_event.set()
        if getattr(self, "watcher", None) is not None:
            self.watcher.stop()
        if self.asr is not None:
            self.asr.done.wait(timeout=timeout)


def main():
    args = parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if args.list_devices:
        list_loopback_devices()
        return
    pipe = Pipeline(args).start()
    try:
        while pipe.alive():
            time.sleep(0.5)
            if not pipe.capture_alive() and not args.file:
                print("[error] audio capture stopped" + (f": {pipe.rec.error}" if getattr(pipe.rec, "error", None) else ""), flush=True)
                break
    except KeyboardInterrupt:
        print("\n[stop] finishing pending utterances ...", flush=True)
        pipe.stop()
    print("[done]", flush=True)


if __name__ == "__main__":
    main()
