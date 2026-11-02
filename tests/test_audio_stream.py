import threading
import time
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

import numpy as np

from audio_receiver import AudioReceiver
from audio_stream import AudioStreamSender, validate_destination, wav_packet


class FixtureRecorder(threading.Thread):
    instances = []

    def __init__(self, browser, target):
        super().__init__(daemon=True)
        self.target = target
        self.stop_event = threading.Event()
        self.error = None
        self.instances.append(self)

    def run(self):
        while not self.stop_event.is_set():
            self.target.put((0, np.full(1600, .25, dtype=np.float32)))
            self.stop_event.wait(.02)
        self.target.put(None)


class AudioStreamTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        self.arrived = threading.Event()
        def receive(pcm):
            self.received.append(pcm)
            self.arrived.set()
        self.receiver = AudioReceiver(receive, '/custom/path/receive', host='127.0.0.1', port=0)
        self.receiver.start()
        self.url = f'http://127.0.0.1:{self.receiver.port}/custom/path/receive'
        self.sender = None
        FixtureRecorder.instances = []

    def tearDown(self):
        if self.sender:
            self.sender.close()
        self.receiver.close()

    def start_sender(self, url=None):
        self.sender = AudioStreamSender(SimpleNamespace(is_open=lambda: True), url or self.url,
                                        recorder_factory=FixtureRecorder)
        self.sender.start()

    def test_sender_pushes_audio_to_exact_mac_path_and_stops_capture(self):
        self.start_sender()
        self.assertTrue(self.arrived.wait(3))
        samples = np.frombuffer(self.received[0], dtype='<i2')
        np.testing.assert_array_equal(samples, np.full(1600, 8191))
        self.sender.close()
        self.assertFalse(self.sender.thread.is_alive())
        self.assertTrue(all(not r.is_alive() for r in FixtureRecorder.instances))
        self.assertEqual(self.sender.status()['state'], 'stopped')

    def test_receiver_status_distinguishes_transport_from_playback(self):
        self.start_sender()
        self.assertTrue(self.arrived.wait(3))
        with urllib.request.urlopen(self.url + '/status', timeout=3) as response:
            import json
            status = json.load(response)
        self.assertGreater(status['received_packets'], 0)
        self.assertEqual(status['peak_db'], -12.04)
        self.assertIsNotNone(status['packet_age_seconds'])

    def test_wrong_path_cannot_play_audio(self):
        req = urllib.request.Request(self.url + '-wrong', data=wav_packet(np.ones(1600)),
                                     headers={'Content-Type': 'audio/wav'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(caught.exception.code, 404)
        self.assertFalse(self.received)

    def test_bad_packet_cannot_reach_output(self):
        req = urllib.request.Request(self.url, data=b'invalid' * 20, headers={'Content-Type': 'audio/wav'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=3)
        self.assertEqual(caught.exception.code, 400)
        self.assertFalse(self.received)

    def test_destination_requires_supported_scheme_host_and_receive_path(self):
        for url in ('', '192.168.1.2/audio', 'ftp://example.test/audio', 'http://example.test',
                    'http://example.test/', 'http://example.test:0/audio',
                    'http://user:password@example.test/audio', 'http://example.test/audio#fragment',
                    'http://example.test/audio\nInjected: yes'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_destination(url)
        url = validate_destination('https://example.test:8767/custom/path?key=test')
        self.assertEqual(url.path, '/custom/path')
        self.assertEqual(url.query, 'key=test')

    def test_connection_recovers_when_output_becomes_available(self):
        self.receiver.close()
        attempts = []
        def failing_once(pcm):
            attempts.append(pcm)
            if len(attempts) == 1:
                raise RuntimeError('Audio output temporarily unavailable')
            self.arrived.set()
        self.receiver = AudioReceiver(failing_once, '/receive', host='127.0.0.1', port=0)
        self.receiver.start()
        self.start_sender(f'http://127.0.0.1:{self.receiver.port}/receive')
        self.assertTrue(self.arrived.wait(5))
        deadline = time.monotonic() + 1
        while self.sender.status()['state'] != 'sending' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.sender.status()['state'], 'sending')
        self.assertGreaterEqual(len(attempts), 2)


if __name__ == '__main__':
    unittest.main()
