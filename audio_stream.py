"""Push received Teams audio to a Mac HTTP endpoint in short WAV packets."""
import http.client
import queue
import struct
import threading
import time
from urllib.parse import urlsplit

import numpy as np


def wav_packet(samples):
    pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
    return struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', len(pcm) + 36, b'WAVE',
                       b'fmt ', 16, 1, 1, 16000, 32000, 2, 16, b'data', len(pcm)) + pcm


def validate_destination(destination):
    if not isinstance(destination, str) or any(c.isspace() for c in destination):
        raise ValueError('Укажите полный адрес Mac без пробелов')
    try:
        url = urlsplit(destination)
        port = url.port
        valid = (url.scheme in ('http', 'https') and url.hostname and
                 not url.username and not url.password and not url.fragment and
                 url.path.startswith('/') and url.path != '/' and (port is None or 1 <= port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Укажите адрес с путём приёма, например http://192.168.1.50:8767/audio/receive')
    return url


class AudioStreamSender:
    def __init__(self, browser, destination, recorder_factory=None):
        self.url = validate_destination(destination)
        self.destination = destination
        self.browser = browser
        if recorder_factory is None:
            from teams_audio import TeamsBrowserRecorder
            recorder_factory = TeamsBrowserRecorder
        self.factory = recorder_factory
        self.closed = threading.Event()
        self._lock = threading.Lock()
        self._state = 'connecting'
        self._error = ''
        self._connection = None
        self.thread = threading.Thread(target=self._run, daemon=True, name='teams-audio-sender')

    def status(self):
        with self._lock:
            return {'running': self.thread.is_alive(), 'destination': self.destination,
                    'state': self._state, 'error': self._error}

    def _set_state(self, state, error=''):
        with self._lock:
            self._state, self._error = state, error

    def start(self):
        self.thread.start()

    def _run(self):
        rec = None
        audio = queue.Queue(maxsize=16)
        try:
            if not self.browser.is_open():
                raise RuntimeError('Откройте встроенный Teams')
            rec = self.factory(self.browser, audio)
            rec.start()
            path = self.url.path + ('?' + self.url.query if self.url.query else '')
            while not self.closed.is_set():
                try:
                    item = audio.get(timeout=.5)
                except queue.Empty:
                    if not rec.is_alive():
                        raise RuntimeError(getattr(rec, 'error', None) or 'Захват звука остановлен')
                    continue
                if item is None:
                    if getattr(rec, 'error', None):
                        raise RuntimeError(rec.error)
                    break
                try:
                    if self._connection is None:
                        cls = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
                        self._connection = cls(self.url.hostname, self.url.port, timeout=3)
                    self._connection.request('POST', path, body=wav_packet(item[1]),
                                             headers={'Content-Type': 'audio/wav', 'Cache-Control': 'no-store'})
                    response = self._connection.getresponse()
                    response.read(1024)
                    if response.status != 204:
                        raise RuntimeError(f'Приёмник ответил HTTP {response.status}')
                    self._set_state('sending')
                except (OSError, http.client.HTTPException, RuntimeError) as exc:
                    self._set_state('retrying', str(exc))
                    if self._connection is not None:
                        self._connection.close()
                    self._connection = None
                    # Discard old packets while reconnecting; do not accumulate delay.
                    retry_at = time.monotonic() + 2
                    while not self.closed.is_set() and time.monotonic() < retry_at:
                        try:
                            pending = audio.get(timeout=.1)
                            if pending is None:
                                raise RuntimeError(getattr(rec, 'error', None) or 'Захват звука остановлен')
                        except queue.Empty:
                            pass
        except Exception as exc:
            self._set_state('error', str(exc))
        finally:
            if self._connection is not None:
                self._connection.close()
            if rec is not None:
                rec.stop_event.set()
                deadline = time.monotonic() + 4
                while rec.is_alive() and time.monotonic() < deadline:
                    try:
                        audio.get(timeout=.1)
                    except queue.Empty:
                        pass
                    rec.join(timeout=.1)
            if self.closed.is_set():
                self._set_state('stopped')

    def close(self):
        self.closed.set()
        if self.thread.is_alive():
            self.thread.join(timeout=8)
