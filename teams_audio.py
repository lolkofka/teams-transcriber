"""Receive PCM from our Teams WKWebView without an audio loopback driver."""
import base64
import json
import queue
import threading
import time
import wave

import numpy as np

from paths import RESOURCE_DIR


def teams_origin(host):
    return host == "teams.microsoft.com" or host.endswith(".teams.microsoft.com") or host == "teams.cloud.microsoft"


class TeamsAudioTap:
    def __init__(self):
        self.installed = threading.Event()
        self.error = None
        self._queue = None
        self._lock = threading.Lock()
        self._handler = None

    def subscribe(self):
        if not self.installed.wait(15):
            raise RuntimeError(self.error or "Не удалось подключить захват звука встроенного Teams")
        with self._lock:
            if self._queue is not None:
                raise RuntimeError("Звук Teams уже записывается")
            self._queue = queue.Queue(maxsize=64)
            return self._queue

    def unsubscribe(self, target):
        with self._lock:
            if self._queue is target:
                self._queue = None

    def receive(self, host, body):
        # The Teams page has no Python API. This bridge accepts bounded PCM only.
        if not teams_origin(host) or not isinstance(body, str) or len(body) > 24000:
            return
        try:
            message = json.loads(body)
            if not isinstance(message, dict):
                return
            if message.get("kind") == "error":
                self.error = str(message.get("error", ""))[:300]
                return
            if message.get("kind") != "pcm":
                return
            rate, stamp = int(message["rate"]), float(message["time"])
            frame = str(message["frame"])
            if not 8000 <= rate <= 192000 or not np.isfinite(stamp) or abs(stamp - time.time()) > 5 or len(frame) > 100:
                return
            pcm = base64.b64decode(message["pcm"], validate=True)
            if not pcm or len(pcm) % 2 or len(pcm) > 16384:
                return
            chunk = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            with self._lock:
                target = self._queue
                if target is not None:
                    if target.full():
                        target.get_nowait()
                    target.put_nowait((frame, rate, stamp, chunk))
        except (ValueError, TypeError, KeyError, OverflowError, queue.Empty, queue.Full):
            return

    def install_macos(self, window):
        """Called synchronously by before_show, before the first navigation."""
        try:
            import WebKit
            from webview.platforms.cocoa import BrowserView
            from pathlib import Path
            controller = BrowserView.instances[window.uid].webview.configuration().userContentController()
            self._handler = _message_handler(self)
            controller.addScriptMessageHandler_name_(self._handler, "teamsAudio")
            source = Path(RESOURCE_DIR, "ui", "teams-audio.js").read_text(encoding="utf-8")
            script = WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
                source, WebKit.WKUserScriptInjectionTimeAtDocumentStart, False)
            controller.addUserScript_(script)
            self.installed.set()
        except Exception as exc:
            self.error = str(exc)


_handler_class = None


def _message_handler(tap):
    global _handler_class
    if _handler_class is None:
        from Foundation import NSObject

        class TeamsPCMMessageHandler(NSObject):
            def userContentController_didReceiveScriptMessage_(self, controller, message):
                origin = message.frameInfo().securityOrigin()
                if origin.protocol() == "https":
                    self.tap.receive(str(origin.host()), message.body())

        _handler_class = TeamsPCMMessageHandler
    handler = _handler_class.alloc().init()
    handler.tap = tap
    return handler


class AudioTimeline:
    """Mix independently timed Teams frames at 16 kHz; gaps remain silence."""
    RATE = 16000

    def __init__(self, start):
        self.start = start
        self.cursor = 0
        self.buffer = np.zeros(self.RATE * 3, dtype=np.float32)
        self.resamplers = {}

    def add(self, frame, rate, stamp, chunk):
        from transcribe import Decimator
        key = (frame, rate)
        if key not in self.resamplers:
            if len(self.resamplers) >= 32:
                self.resamplers.pop(next(iter(self.resamplers)))
            self.resamplers[key] = Decimator(rate, self.RATE)
        samples = self.resamplers[key](chunk)
        index = round((stamp - self.start) * self.RATE)
        left, right = max(index, self.cursor), min(index + len(samples), self.cursor + len(self.buffer))
        if right > left:
            indices = np.arange(left, right) % len(self.buffer)
            self.buffer[indices] += samples[left - index:right - index]

    def read(self, size):
        indices = np.arange(self.cursor, self.cursor + size) % len(self.buffer)
        chunk = np.clip(self.buffer[indices], -1, 1)
        self.buffer[indices] = 0
        self.cursor += size
        return chunk


class TeamsBrowserRecorder(threading.Thread):
    def __init__(self, browser, out_queue, wav_path=None):
        super().__init__(daemon=True, name="TeamsBrowserAudio")
        self.browser, self.q, self.wav_path = browser, out_queue, wav_path
        self.tap = browser.audio
        self.stop_event = threading.Event()
        self.device_name = None
        self.error = None
        self.level = 0.0
        self.monitor = None  # Teams already plays the received audio normally.

    def run(self):
        target = wav = None
        try:
            target = self.tap.subscribe()
            self.device_name = "Teams — звук встроенного окна"
            timeline = AudioTimeline(time.time())
            if self.wav_path:
                wav = wave.open(self.wav_path, "wb")
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
            from transcribe import CON
            CON.final("[audio] ожидаю входящий звук собрания во встроенном Teams")
            last_audio = time.monotonic()
            warned = False
            while not self.stop_event.wait(0.02):
                if not self.browser.is_open() or self.browser.audio is not self.tap:
                    raise RuntimeError("Окно Teams закрыто; запись звука остановлена")
                if self.tap.error:
                    raise RuntimeError("Захват Teams: " + self.tap.error)
                for _ in range(64):
                    try:
                        timeline.add(*target.get_nowait())
                        last_audio = time.monotonic()
                    except queue.Empty:
                        break
                if not warned and time.monotonic() - last_audio > 20:
                    CON.final("[audio] звук Teams пока не поступает: откройте собрание во встроенном окне и нажмите в нём любую кнопку")
                    warned = True
                # A short playout delay absorbs native bridge scheduling jitter.
                available = int((time.time() - timeline.start - 0.25) * 16000)
                while available - timeline.cursor >= 1600:
                    stamp = timeline.start + timeline.cursor / 16000
                    chunk = timeline.read(1600)
                    self.level = float(np.max(np.abs(chunk)))
                    if wav:
                        wav.writeframes((chunk * 32767).astype("<i2").tobytes())
                    self.q.put((stamp, chunk))
        except Exception as exc:
            self.error = str(exc)
            from transcribe import CON
            CON.final("[error] " + self.error)
        finally:
            if target is not None:
                self.tap.unsubscribe(target)
            if wav:
                wav.close()
            self.level = 0.0
            self.q.put(None)
