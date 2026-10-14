import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from vcam import CamRecorder


class FakeCamera:
    def __init__(self,black=False):self.black=black;self.released=False;self.count=0
    def isOpened(self):return True
    def set(self,*args):return True
    def release(self):self.released=True
    def read(self):
        time.sleep(.02);self.count+=1
        frame=np.zeros((120,160,3),dtype=np.uint8)
        if not self.black:frame[:,:,1]=80+(self.count%100)
        return True,frame


class CameraRecordingTests(unittest.TestCase):
    def test_native_chunks_finalize_only_readable_video(self):
        import base64
        import app
        with tempfile.TemporaryDirectory() as folder,patch('app.HERE',folder),patch('app.BASE_DIR',folder),patch('app.SETTINGS',str(Path(folder,'settings.json'))):
            source=str(Path(folder,'source.mp4'))
            writer=cv2.VideoWriter(source,cv2.VideoWriter_fourcc(*'mp4v'),10,(160,120))
            for _ in range(30):writer.write(np.full((120,160,3),100,dtype=np.uint8))
            writer.release();data=Path(source).read_bytes()
            api=app.Api();session=api.cam_native_begin(3)['id']
            self.assertFalse(api.cam_native_chunk('wrong',base64.b64encode(data).decode())['ok'])
            for part in (data[:500],data[500:]):
                self.assertTrue(api.cam_native_chunk(session,base64.b64encode(part).decode())['ok'])
            result=api.cam_native_end(session)
            self.assertTrue(result['ok'],result)
            self.assertEqual(Path(result['file']).read_bytes(),data)
            self.assertEqual(api.settings()['vcam_file'],result['file'])
            self.assertFalse(api.cam_native_end(session)['ok'])

    def test_live_preview_and_decodable_mp4(self):
        real=cv2.VideoCapture;camera=FakeCamera();events=[]
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder,'record.mp4'))
            with patch('cv2.VideoCapture',side_effect=lambda source,*args:camera if isinstance(source,int) else real(source)):
                recorder=CamRecorder(0,3,path,events.append);recorder.run()
            self.assertTrue(recorder.result['ok'],recorder.result)
            self.assertTrue(camera.released)
            self.assertTrue(any(e['state']=='preview' and e['image'].startswith('data:image/jpeg;base64,') for e in events))
            cap=real(path);ok,frame=cap.read();cap.release()
            self.assertTrue(ok and frame.any())
            self.assertGreater(recorder.result['frames'],50)

    def test_black_frames_are_not_reported_as_success(self):
        camera=FakeCamera(black=True);clock=[0.0]
        def tick():clock[0]+=.3;return clock[0]
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder,'black.mp4'))
            with patch('cv2.VideoCapture',return_value=camera),patch('vcam.time.monotonic',side_effect=tick),patch('vcam.time.sleep'):
                recorder=CamRecorder(0,3,path);recorder.run()
            self.assertFalse(recorder.result['ok'])
            self.assertIn('чёрные',recorder.result['error'])
            self.assertFalse(Path(path).exists())
            self.assertTrue(camera.released)

    def test_writer_open_failure_is_visible(self):
        camera=FakeCamera();writer=unittest.mock.Mock();writer.isOpened.return_value=False
        with tempfile.TemporaryDirectory() as folder,patch('cv2.VideoCapture',return_value=camera),patch('cv2.VideoWriter',return_value=writer):
            recorder=CamRecorder(0,3,str(Path(folder,'failed.mp4')));recorder.run()
        self.assertFalse(recorder.result['ok'])
        self.assertIn('MP4',recorder.result['error'])
        self.assertTrue(camera.released)


if __name__=='__main__':unittest.main()
