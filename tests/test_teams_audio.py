import base64
import json
import queue
import tempfile
import time
import unittest
import wave
from types import SimpleNamespace

import numpy as np

from teams_audio import AudioTimeline, TeamsAudioTap, TeamsBrowserRecorder


class TeamsAudioTests(unittest.TestCase):
    def packet(self, **overrides):
        packet = dict(kind="pcm", rate=16000, time=time.time(), frame="frame1",
                      pcm=base64.b64encode(np.full(1600, 8192, dtype="<i2").tobytes()).decode())
        packet.update(overrides)
        return json.dumps(packet)

    def test_bridge_accepts_only_teams_pcm_and_has_bounded_backlog(self):
        tap = TeamsAudioTap(); tap.installed.set()
        target = tap.subscribe()
        for host in ("evil.test", "teams.microsoft.com.evil.test"):
            tap.receive(host, self.packet())
        for body in ("not JSON", self.packet(rate=0), self.packet(pcm="bad"), self.packet(time=float("nan"))):
            tap.receive("teams.microsoft.com", body)
        self.assertTrue(target.empty())
        for _ in range(80):
            tap.receive("teams.cloud.microsoft", self.packet())
        self.assertEqual(target.qsize(), 64)
        frame, rate, stamp, chunk = target.get_nowait()
        self.assertEqual(rate, 16000)
        np.testing.assert_allclose(chunk, .25)
        tap.unsubscribe(target)
        replacement = tap.subscribe()
        tap.unsubscribe(target)  # old recorder cannot detach a new recording
        tap.receive("teams.microsoft.com", self.packet())
        self.assertEqual(replacement.qsize(), 1)

    def test_timeline_mixes_frames_without_speeding_up_and_preserves_gaps(self):
        timeline = AudioTimeline(100)
        timeline.add("a", 16000, 100, np.full(1600, .2, np.float32))
        timeline.add("b", 16000, 100, np.full(1600, .3, np.float32))
        np.testing.assert_allclose(timeline.read(1600), .5)
        np.testing.assert_equal(timeline.read(1600), 0)
        timeline.add("a", 16000, 100, np.ones(1600, np.float32))  # stale audio is discarded
        np.testing.assert_equal(timeline.read(1600), 0)

    def test_resampling_and_ring_wrap_do_not_repeat_audio(self):
        timeline = AudioTimeline(100)
        timeline.add("a", 48000, 100, np.ones(4800, np.float32) * .3)
        chunk = timeline.read(1600)
        self.assertAlmostEqual(float(chunk[-1]), .3, places=3)
        timeline.read(16000 * 3 - 1600)
        np.testing.assert_equal(timeline.read(1600), 0)

    def test_stop_finishes_wav_and_queue_without_closing_teams(self):
        tap = TeamsAudioTap(); tap.installed.set()
        browser = SimpleNamespace(audio=tap, is_open=lambda: True)
        target = queue.Queue()
        with tempfile.TemporaryDirectory() as folder:
            path = folder + "/recording.wav"
            rec = TeamsBrowserRecorder(browser, target, path)
            rec.start()
            deadline = time.monotonic() + 3
            while not rec.device_name and time.monotonic() < deadline:
                time.sleep(.01)
            for _ in range(8):
                tap.receive("teams.microsoft.com", self.packet())
                time.sleep(.05)
            rec.stop_event.set(); rec.join(3)
            self.assertFalse(rec.is_alive())
            self.assertIsNone(rec.error)
            chunks = []
            while not target.empty():
                item = target.get_nowait()
                if item is not None:
                    chunks.append(item[1])
            self.assertGreater(np.max(np.abs(np.concatenate(chunks))), .1)
            with wave.open(path) as wav:
                self.assertEqual(wav.getframerate(), 16000)
                self.assertGreater(wav.getnframes(), 0)
            self.assertIsNone(tap._queue)


if __name__ == "__main__":
    unittest.main()
