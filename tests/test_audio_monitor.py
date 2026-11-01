import threading
import time
import unittest
from types import SimpleNamespace

from audio_monitor import AudioMonitor, monitor_devices


class FakeCapture(threading.Thread):
    instances=[]

    def __init__(self, source, queue, wav_path=None, monitor_index=None, monitor_volume=1):
        super().__init__(daemon=True)
        assert wav_path is None, 'Idle playback must not create a recording'
        self.source=source
        self.output=monitor_index
        self.monitor=SimpleNamespace(volume=monitor_volume, is_alive=lambda: True,
                                     last_write_at=time.monotonic(), error=None)
        self.stop_event=threading.Event()
        self.error=None
        self.last_frame_at=time.monotonic()
        self.last_signal_at=self.last_frame_at
        self.queue=queue
        self.instances.append(self)

    def run(self):
        while not self.stop_event.wait(.01):
            self.last_frame_at=time.monotonic()
            self.monitor.last_write_at=self.last_frame_at
            self.queue.put(('audio',))


class MonitorTests(unittest.TestCase):
    def test_playback_without_recording_and_live_output_change(self):
        FakeCapture.instances=[]
        cfg={'input':'1','output':'2','volume':50}
        monitor=AudioMonitor(lambda:dict(cfg),lambda _:None,FakeCapture)
        monitor.start()
        def wait(predicate):
            deadline=time.monotonic()+2
            while not predicate() and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertTrue(predicate())
        try:
            wait(lambda:monitor.rec is not None and monitor.rec.is_alive())
            first=monitor.rec
            self.assertEqual(first.monitor.volume,.5)
            cfg['volume']=20
            wait(lambda:first.monitor.volume==.2)
            cfg['output']='3'
            wait(lambda:monitor.rec is not None and monitor.rec.output==3)
            self.assertFalse(first.is_alive())
            cfg['output']='none'
            wait(lambda:monitor.rec is None)
        finally:
            monitor.close()
        self.assertTrue(all(not rec.is_alive() for rec in FakeCapture.instances))

    def test_embedded_teams_is_not_played_twice(self):
        self.assertIsNone(monitor_devices({'input':'teams-browser','output':'2'}))
        self.assertIsNone(monitor_devices({'input':'1','output':'none'}))

    def test_idle_silence_reopens_capture_automatically(self):
        FakeCapture.instances=[]
        logs=[]
        monitor=AudioMonitor(lambda:{'input':'1','output':'2'},logs.append,FakeCapture)
        try:
            monitor.update({'input':'1','output':'2'})
            first=monitor.rec
            for _ in range(100):
                if first.is_alive():
                    break
                time.sleep(.001)
            self.assertTrue(first.is_alive())
            first.last_signal_at=time.monotonic()-monitor.IDLE_REOPEN_SECONDS-1
            monitor.update({'input':'1','output':'2'})
            self.assertIsNot(monitor.rec,first)
            self.assertFalse(first.is_alive())
            self.assertTrue(any('тишину' in message for message in logs))
        finally:
            monitor._stop_capture()

    def test_dead_output_reopens_capture_automatically(self):
        monitor=AudioMonitor(lambda:{'input':'1','output':'2'},lambda _:None,FakeCapture)
        try:
            monitor.update({'input':'1','output':'2'})
            first=monitor.rec
            for _ in range(100):
                if first.is_alive():
                    break
                time.sleep(.001)
            self.assertTrue(first.is_alive())
            first.monitor.is_alive=lambda: False
            first.monitor.error='output device lost'
            monitor.update({'input':'1','output':'2'})
            self.assertIsNone(monitor.rec)
            self.assertFalse(first.is_alive())
            monitor._retry_at=0
            monitor.update({'input':'1','output':'2'})
            self.assertIsNot(monitor.rec,first)
        finally:
            monitor._stop_capture()


if __name__=='__main__':
    unittest.main()
