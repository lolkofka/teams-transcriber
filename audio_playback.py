"""Bounded jitter buffer and recoverable, continuously clocked PCM16 output."""
import threading
import time


class PCMBuffer:
    RATE = 16000
    FRAME_BYTES = 2

    def __init__(self, prefill_ms=160, max_ms=500, clock=time.monotonic):
        if not 0 < prefill_ms < max_ms:
            raise ValueError('Prefill must be positive and smaller than the maximum buffer')
        self.prefill = int(prefill_ms * self.RATE / 1000) * self.FRAME_BYTES
        self.limit = int(max_ms * self.RATE / 1000) * self.FRAME_BYTES
        self.clock = clock
        self.lock = threading.Lock()
        self.data = bytearray()
        self.playing = False
        self.last_packet = None
        self.underruns = 0
        self.dropped_frames = 0
        self.played_frames = 0

    def push(self, pcm):
        if len(pcm) % self.FRAME_BYTES:
            raise ValueError('Incomplete PCM16 frame')
        with self.lock:
            now = self.clock()
            if self.last_packet is not None and now - self.last_packet > 1:
                self.dropped_frames += len(self.data) // self.FRAME_BYTES
                self.data.clear()
                self.playing = False
            self.last_packet = now
            self.data.extend(pcm)
            if len(self.data) > self.limit:
                # After a burst or a clock drift, keep recent audio rather than
                # making the listener hear an ever-growing backlog.
                drop = len(self.data) - self.prefill
                self.dropped_frames += drop // self.FRAME_BYTES
                del self.data[:drop]

    def read(self, frames):
        size = frames * self.FRAME_BYTES
        with self.lock:
            if not self.playing:
                if len(self.data) < max(self.prefill, size):
                    return bytes(size)
                self.playing = True
            available = min(size, len(self.data))
            result = bytes(self.data[:available])
            self.played_frames += available // self.FRAME_BYTES
            del self.data[:available]
            if available < size:
                self.underruns += 1
                self.playing = False
                result += bytes(size - available)
            return result

    def reset(self):
        with self.lock:
            self.dropped_frames += len(self.data) // self.FRAME_BYTES
            self.data.clear()
            self.playing = False

    def status(self):
        with self.lock:
            return {'buffer_ms': round(len(self.data) / self.FRAME_BYTES / self.RATE * 1000, 1),
                    'underruns': self.underruns, 'dropped_frames': self.dropped_frames,
                    'played_frames': self.played_frames}


class AudioPlayback:
    def __init__(self, output_index=None, output_name=None, prefill_ms=160,
                 backend=None, on_log=print, retry_seconds=1, callback_timeout=2):
        self.output_index = output_index
        self.output_name = output_name
        self.backend = backend
        self.on_log = on_log
        self.retry_seconds = retry_seconds
        self.callback_timeout = callback_timeout
        self.buffer = PCMBuffer(prefill_ms=prefill_ms, max_ms=max(500, prefill_ms + 100))
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.state = 'opening'
        self.error = ''
        self.reopens = 0
        self.device_underflows = 0
        self.callback_count = 0
        self.callback_max_frames = 0
        self.last_callback = None
        self.manager = None
        self.stream = None
        self.thread = threading.Thread(target=self._run, daemon=True, name='audio-playback')

    def _set_state(self, state, error=''):
        with self.lock:
            self.state, self.error = state, error

    def status(self):
        with self.lock:
            result = {'state': self.state, 'error': self.error, 'output': self.output_name,
                      'reopens': self.reopens, 'device_underflows': self.device_underflows,
                      'callback_count': self.callback_count, 'callback_max_frames': self.callback_max_frames}
        return dict(result, **self.buffer.status())

    def _callback(self, in_data, frame_count, time_info, flags):
        self.callback_count += 1
        self.callback_max_frames = max(self.callback_max_frames, frame_count)
        self.last_callback = time.monotonic()
        if flags & self.backend.paOutputUnderflow:
            self.device_underflows += 1
        return self.buffer.read(frame_count), self.backend.paContinue

    def _open(self):
        self.last_callback = time.monotonic()
        self.manager = self.backend.PyAudio()
        if self.output_name is None:
            info = (self.manager.get_default_output_device_info() if self.output_index is None
                    else self.manager.get_device_info_by_index(self.output_index))
            if not info['maxOutputChannels']:
                raise ValueError('Выбрано устройство без аудиовыхода')
            self.output_name = info['name']
        matches = [self.manager.get_device_info_by_index(i)
                   for i in range(self.manager.get_device_count())]
        matches = [d for d in matches if d['name'] == self.output_name and d['maxOutputChannels']]
        if len(matches) != 1:
            raise OSError('Выбранный аудиовыход недоступен или его имя неоднозначно')
        # Resolve the saved name on every reopen: Bluetooth and virtual-device
        # changes can reorder PortAudio indices. Never fall back to speakers.
        self.stream = self.manager.open(format=self.backend.paInt16, channels=1,
                                        rate=16000, output=True,
                                        output_device_index=matches[0]['index'],
                                        frames_per_buffer=320, stream_callback=self._callback)

    def _close_device(self):
        stream, self.stream = self.stream, None
        manager, self.manager = self.manager, None
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if manager is not None:
            try:
                manager.terminate()
            except Exception:
                pass

    def _run(self):
        try:
            if self.backend is None:
                import pyaudio
                self.backend = pyaudio
            last_error = None
            while not self.closed.is_set():
                try:
                    self._open()
                    self._set_state('playing')
                    self.ready.set()
                    self.on_log('[audio] Выход: ' + self.output_name)
                    last_error = None
                    while not self.closed.wait(.2):
                        if not self.stream.is_active():
                            raise OSError('Аудиовыход остановился')
                        if time.monotonic() - self.last_callback > self.callback_timeout:
                            raise OSError('Аудиовыход не запрашивает данные')
                except Exception as exc:
                    self.reopens += 1
                    self._set_state('recovering', str(exc))
                    self.ready.set()
                    if str(exc) != last_error:
                        self.on_log('[audio] Восстановление выхода: ' + str(exc))
                        last_error = str(exc)
                finally:
                    self._close_device()
                    self.buffer.reset()
                if not self.closed.is_set():
                    self.closed.wait(self.retry_seconds)
        except Exception as exc:
            self._set_state('error', str(exc))
            self.ready.set()
        finally:
            self._close_device()
            if self.closed.is_set():
                self._set_state('stopped')

    def start(self):
        self.thread.start()
        self.ready.wait(5)

    def write(self, pcm):
        state = self.status()
        if self.closed.is_set() or state['state'] != 'playing':
            raise OSError(state['error'] or 'Аудиовыход ещё не готов')
        self.buffer.push(pcm)

    def close(self):
        self.closed.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
