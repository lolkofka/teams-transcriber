"""GUI audio passthrough, independent of the recording/transcription pipeline."""
import threading
import time


class _Discard:
    def put(self, item):
        pass


def monitor_devices(cfg):
    source, output = cfg.get('input'), cfg.get('output')
    # Embedded Teams has its own normal speaker output; forwarding it would duplicate audio.
    if source in (None, '', 'teams-browser') or output in (None, '', 'none'):
        return None
    return int(source), int(output)


class AudioMonitor(threading.Thread):
    IDLE_REOPEN_SECONDS = 60
    OUTPUT_STALL_SECONDS = 15

    def __init__(self, settings, on_log, recorder_factory=None):
        super().__init__(daemon=True, name='audio-monitor')
        self.settings = settings
        self.on_log = on_log
        self.factory = recorder_factory
        self.stop_event = threading.Event()
        self.rec = None
        self._key = None
        self._retry_at = 0
        self._last_error = None

    def set_volume(self, percent):
        if self.rec is not None and self.rec.monitor is not None:
            self.rec.monitor.volume = max(0, min(1, float(percent)/100))

    def _stop_capture(self):
        rec = self.rec
        if rec is not None:
            rec.stop_event.set()
            rec.join(timeout=0.3)
            if rec.is_alive() and hasattr(rec, 'request_stop'):
                rec.request_stop()
                rec.join(timeout=3)
            if not rec.is_alive():
                self.rec = None

    def _capture_problem(self, rec):
        if not rec.is_alive():
            return getattr(rec, 'monitor_error', None) or rec.error or 'захват завершился'
        if getattr(rec, 'monitor_error', None):
            return rec.monitor_error
        output = rec.monitor
        if output is not None:
            if not output.is_alive():
                return output.error or 'поток вывода завершился'
            last_frame = getattr(rec, 'last_frame_at', None)
            last_write = getattr(output, 'last_write_at', None)
            now = time.monotonic()
            if last_frame is not None and last_write is not None and now - last_frame < self.OUTPUT_STALL_SECONDS and now - last_write > self.OUTPUT_STALL_SECONDS:
                return 'поток вывода перестал воспроизводить звук'
        now = time.monotonic()
        last_frame = getattr(rec, 'last_frame_at', None)
        if last_frame is not None and now - last_frame > self.IDLE_REOPEN_SECONDS:
            return 'захват не получает данные после простоя'
        last_signal = getattr(rec, 'last_signal_at', None)
        if last_signal is not None and now - last_signal > self.IDLE_REOPEN_SECONDS:
            return 'захват долго получает только тишину'
        return None

    def update(self, cfg):
        key = monitor_devices(cfg)
        if key != self._key:
            self._stop_capture()
            self._key, self._retry_at, self._last_error = key, 0, None
        if self.rec is not None and self.rec.is_alive() and self.rec.stop_event.is_set():
            self._stop_capture()
            if self.rec is not None:
                return
        if key is None:
            return
        error = self._capture_problem(self.rec) if self.rec is not None else None
        if error is not None:
            self._stop_capture()
            self._retry_at = time.monotonic()+10
            if error != self._last_error:
                self._last_error = error
                self.on_log('[audio] прослушивание: '+error)
        if self.rec is None and time.monotonic() >= self._retry_at:
            from transcribe import Recorder
            factory = self.factory or Recorder
            source, output = key
            self.rec = factory(source, _Discard(), wav_path=None, monitor_index=output,
                               monitor_volume=max(0, min(1, float(cfg.get('volume',100))/100)))
            self.rec.start()
            self.on_log('[audio] прослушивание включено независимо от записи')
        self.set_volume(cfg.get('volume', 100))

    def run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.update(self.settings())
                except Exception as exc:
                    error = str(exc)
                    if error != self._last_error:
                        self._last_error = error
                        self.on_log('[audio] прослушивание: '+error)
                self.stop_event.wait(0.5)
        finally:
            self._stop_capture()

    def close(self):
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=4)
