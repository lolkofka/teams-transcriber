import struct
import time
import unittest
from types import SimpleNamespace

from audio_playback import AudioPlayback, PCMBuffer


def pcm(value, frames):
    return struct.pack('<h', value) * frames


class PCMBufferTests(unittest.TestCase):
    def test_jitter_prefill_preserves_sample_order_and_refills_after_a_gap(self):
        buffer = PCMBuffer(prefill_ms=40)
        buffer.push(pcm(100, 160))
        self.assertEqual(buffer.read(320), bytes(640))
        buffer.push(pcm(200, 480))
        self.assertEqual(buffer.read(320), pcm(100, 160) + pcm(200, 160))
        self.assertEqual(buffer.read(320), pcm(200, 320))
        self.assertEqual(buffer.read(320), bytes(640))
        self.assertEqual(buffer.status()['underruns'], 1)
        buffer.push(pcm(300, 320))
        self.assertEqual(buffer.read(320), bytes(640))
        buffer.push(pcm(400, 320))
        self.assertEqual(buffer.read(320), pcm(300, 320))

    def test_network_burst_drops_old_audio_instead_of_accumulating_latency(self):
        buffer = PCMBuffer(prefill_ms=40, max_ms=200)
        buffer.push(pcm(100, 3000))
        buffer.push(pcm(200, 1000))
        self.assertLessEqual(buffer.status()['buffer_ms'], 200)
        self.assertEqual(buffer.read(640), pcm(200, 640))
        self.assertGreater(buffer.status()['dropped_frames'], 0)

    def test_long_connection_gap_discards_stale_audio(self):
        now = [0]
        buffer = PCMBuffer(prefill_ms=40, clock=lambda: now[0])
        buffer.push(pcm(100, 640))
        now[0] = 2
        buffer.push(pcm(200, 640))
        self.assertEqual(buffer.read(640), pcm(200, 640))

    def test_callback_reads_always_have_requested_length(self):
        buffer = PCMBuffer(prefill_ms=40)
        for frames in (160, 320, 512, 1600):
            buffer.push(pcm(100, 700))
            self.assertEqual(len(buffer.read(frames)), frames * 2)


class FakeStream:
    def __init__(self):
        self.active = True
        self.closed = False

    def is_active(self):
        return self.active

    def stop_stream(self):
        raise OSError('CoreAudio stream was invalidated')

    def close(self):
        self.closed = True


class PlaybackTests(unittest.TestCase):
    def backend(self):
        managers, opened, streams = [], [], []

        class Manager:
            def __init__(self):
                self.generation = len(managers)
                self.terminated = False
                managers.append(self)

            def get_device_count(self):
                return 6 if self.generation == 0 else 3

            def get_device_info_by_index(self, index):
                target = 5 if self.generation == 0 else 2
                return {'index': index, 'name': 'EShareAudio' if index == target else 'Other',
                        'maxOutputChannels': 2 if index == target else 0}

            def open(self, **kwargs):
                opened.append(kwargs)
                stream = FakeStream()
                streams.append(stream)
                return stream

            def terminate(self):
                self.terminated = True

        backend = SimpleNamespace(PyAudio=Manager, paInt16=8, paContinue=0, paOutputUnderflow=4)
        return backend, managers, opened, streams

    def test_dead_stream_reopens_same_device_by_name_after_indices_change(self):
        backend, managers, opened, streams = self.backend()
        output = AudioPlayback(output_index=5, backend=backend, on_log=lambda _: None,
                               retry_seconds=.01)
        output.start()
        try:
            self.assertEqual(output.status()['state'], 'playing')
            streams[0].active = False
            deadline = time.monotonic() + 2
            while len(streams) < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual([d['output_device_index'] for d in opened], [5, 2])
            self.assertTrue(streams[0].closed)
            self.assertTrue(managers[0].terminated)
            self.assertGreater(output.status()['reopens'], 0)
        finally:
            output.close()
        self.assertFalse(output.thread.is_alive())
        self.assertTrue(all(s.closed for s in streams))
        self.assertTrue(all(m.terminated for m in managers))

    def test_callback_never_ends_when_network_is_silent(self):
        backend, _, opened, _ = self.backend()
        output = AudioPlayback(output_index=5, backend=backend, on_log=lambda _: None)
        output.start()
        try:
            callback = opened[0]['stream_callback']
            data, flag = callback(None, 320, {}, backend.paOutputUnderflow)
            self.assertEqual(data, bytes(640))
            self.assertEqual(flag, backend.paContinue)
            self.assertEqual(output.status()['device_underflows'], 1)
            output.write(pcm(500, 3200))
            data, flag = callback(None, 320, {}, 0)
            self.assertEqual(data, pcm(500, 320))
            self.assertEqual(flag, backend.paContinue)
        finally:
            output.close()

    def test_active_device_without_callbacks_is_reopened(self):
        backend, _, _, streams = self.backend()
        output = AudioPlayback(output_index=5, backend=backend, on_log=lambda _: None,
                               retry_seconds=.01, callback_timeout=.1)
        output.start()
        try:
            deadline = time.monotonic() + 2
            while len(streams) < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertGreaterEqual(len(streams), 2)
            self.assertTrue(streams[0].active)
            self.assertTrue(streams[0].closed)
            self.assertEqual(output.status()['callback_count'], 0)
        finally:
            output.close()


if __name__ == '__main__':
    unittest.main()
