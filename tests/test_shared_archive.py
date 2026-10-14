import datetime as dt
import io
import json
import os
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError

from archive_server import ArchiveHTTP
from archive_sync import ArchiveClient, ArchiveSync, atomic_json, fingerprint
from shared_archive import digest


class ArchiveIntegration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.server=ArchiveHTTP(('127.0.0.1',0),self.root/'server','test-token-with-at-least-24-characters')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client=ArchiveClient('http://127.0.0.1:'+str(self.server.server_port),self.server.token)

    def tearDown(self):
        self.client.close()
        self.server.shutdown();self.server.server_close();self.server.store.db.close();self.temp.cleanup()

    def worker(self,name):
        root=self.root/name/'recordings'
        worker=ArchiveSync(root,self.root/name/'state.json',lambda:{},lambda:None,lambda _:None)
        folder=root/'Blazor'/'2026-10-01_11-30';folder.mkdir(parents=True)
        atomic_json(folder/'lesson.json',dict(subject='Blazor',date='2026-10-01',time='11:30-12:50'))
        return worker,folder

    def test_two_clients_full_archive_text_pull_and_three_day_cleanup(self):
        first,folder=self.worker('first')
        ts=dt.datetime(2026,10,1,11,40).timestamp()
        speech=dict(ts=ts,speaker='SPEAKER_00',source='teams',text='Создайте новый компонент на странице')
        chat=dict(ts=ts+40,speaker='Преподаватель',source='chat',text='Задание App.razor',chat_key='same-chat')
        (folder/'transcript.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in (speech,chat))+'\n',encoding='utf-8')
        (folder/'audio.wav').write_bytes(b'original audio, no compression'*5000)
        (folder/'slides').mkdir();(folder/'slides'/'1.png').write_bytes(b'original slide')
        for path in folder.rglob('*'):
            if path.is_file():os.utime(path,(ts,ts))
        first.sync_folder(self.client,folder)
        self.assertFalse((folder/'audio.wav').exists())
        self.assertTrue((folder/'transcript.jsonl').exists())
        second,other=self.worker('second')
        (other/'transcript.jsonl').write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in (dict(speech,text=speech['text']+' и добавьте кнопку'),chat))+'\n',encoding='utf-8')
        second.sync_folder(self.client,other)
        meetings=self.client.json('/api/meetings');self.assertEqual(len(meetings),1)
        meeting=meetings[0]['meeting'];self.assertEqual(meetings[0]['clients'],2)
        merged=self.client.json('/api/meetings/'+meeting+'/transcript.json')['records']
        self.assertEqual(len(merged),2)
        self.assertIn('добавьте кнопку',merged[0]['text'])
        self.assertTrue(merged[0]['alternatives'])
        second.pull(self.client)
        cached=second.root/'shared'/meeting
        self.assertTrue((cached/'transcript.txt').exists())
        self.assertFalse(list(cached.rglob('*.wav')))
        with self.client.request('/api/meetings/'+meeting+'/full.tar') as response:data=response.read()
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            audio=next(m for m in archive.getmembers() if m.name.endswith('/audio.wav'))
            self.assertEqual(archive.extractfile(audio).read(),b'original audio, no compression'*5000)
        # Repeat sync after local cleanup keeps the server's original media manifest.
        first.sync_folder(self.client,folder)
        manifests=self.client.json('/api/meetings/'+meeting+'/manifest')
        self.assertTrue(any(f['path']=='audio.wav' for m in manifests for f in m['files']))

    def test_resume_authentication_and_unconfirmed_cleanup(self):
        with self.assertRaises(HTTPError) as error:
            ArchiveClient(self.client.url,'wrong').json('/api/meetings')
        self.assertEqual(error.exception.code,401)
        data=b'abcdefghij'*100000;sha=digest(data)
        with self.client.request('/api/blobs/'+sha,data[:1000],'PUT',{'Content-Range':f'bytes 0-999/{len(data)}'}) as response:
            self.assertFalse(json.load(response)['complete'])
        path=self.root/'source.wav';path.write_bytes(data)
        self.client.upload(path,sha,len(data))
        self.assertEqual(self.server.store.blob(sha).read_bytes(),data)
        worker,folder=self.worker('cleanup');audio=folder/'audio.wav';audio.write_bytes(b'not uploaded')
        worker.cleanup(folder,{'date':'2026-10-01','time':'11:30-12:50'}, {})
        self.assertTrue(audio.exists())
        item=dict(sha256=digest(audio.read_bytes()),size=audio.stat().st_size)
        worker.cleanup(folder,{'date':'2026-10-01','time':'11:30-12:50'},{'audio.wav':item},active=folder.resolve())
        self.assertTrue(audio.exists())
        empty=self.root/'empty.txt';empty.touch();self.client.upload(empty,digest(b''),0)
        self.assertTrue(self.server.store.blob(digest(b'')).exists())

    def test_lost_chunk_response_resumes_without_repeating_bytes(self):
        from http.client import RemoteDisconnected
        original=self.client.request
        lost=[False]
        def request(path,body=None,method=None,headers=None):
            response=original(path,body,method,headers)
            if method=='PUT' and not lost[0]:
                response.read();response.close();lost[0]=True
                raise RemoteDisconnected('Simulated lost acknowledgement')
            return response
        self.client.request=request
        data=os.urandom(700000);path=self.root/'audio.wav';path.write_bytes(data)
        from unittest.mock import patch
        with patch('archive_sync.time.sleep'):
            self.client.upload(path,digest(data),len(data))
        self.assertTrue(lost[0])
        self.assertEqual(self.server.store.blob(digest(data)).read_bytes(),data)

    def test_one_time_browser_login_and_followup_request(self):
        import http.cookiejar
        from urllib.request import build_opener,HTTPCookieProcessor
        ticket=self.client.json('/api/ticket',{})['ticket']
        # A second request on the same connection detects unread POST data.
        self.assertEqual(self.client.json('/api/meetings'),[])
        opener=build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with opener.open(self.client.url+'/ticket/'+ticket) as response:
            self.assertIn('Архив пар',response.read().decode())
        with opener.open(self.client.url+'/api/meetings') as response:
            self.assertEqual(json.load(response),[])
        with self.assertRaises(HTTPError):opener.open(self.client.url+'/ticket/'+ticket)

    def test_fifteen_clients_share_one_meeting(self):
        meeting=digest(b'same meeting')
        for number in range(15):
            self.client.json('/api/manifests',dict(meeting=meeting,client='client-'+str(number),files=[],meta={'subject':'Shared lesson'}))
        index=self.client.json('/api/meetings')
        self.assertEqual(len(index),1)
        self.assertEqual(index[0]['clients'],15)

    def test_cleanup_keeps_changed_files_and_every_text_format(self):
        worker,folder=self.worker('retention')
        meta={'date':'2026-10-01','time':'11:30-12:50'}
        audio=folder/'audio.wav';audio.write_bytes(b'first')
        receipt={'audio.wav':dict(sha256=digest(b'first'),size=5)}
        end=dt.datetime(2026,10,1,12,50).timestamp()
        worker.cleanup(folder,meta,receipt,now=end+3*86400-1)
        self.assertTrue(audio.exists())
        audio.write_bytes(b'other')
        worker.cleanup(folder,meta,receipt,now=end+3*86400+1)
        self.assertTrue(audio.exists())
        for name in ('chat.txt','transcript.json','transcript.jsonl','notes.md'):
            path=folder/name;path.write_bytes(b'text')
            receipt[name]=dict(sha256=digest(b'text'),size=4)
        worker.cleanup(folder,meta,receipt,now=end+4*86400)
        self.assertTrue(all((folder/name).exists() for name in receipt))

    def test_restart_preserves_client_and_receipts_with_russian_paths(self):
        worker,folder=self.worker('restart')
        worker.state['receipts']['Предмет/пара/аудио.wav']={'confirmed': True}
        worker.save()
        restored=ArchiveSync(worker.root,worker.state_path,lambda:{},lambda:None,lambda _:None)
        self.assertEqual(restored.state,worker.state)

    def test_one_sync_process_per_installation(self):
        from archive_sync import sync_lock
        path=self.root/'sync.lock'
        with sync_lock(path) as first:
            self.assertTrue(first)
            with sync_lock(path) as second:self.assertFalse(second)
        with sync_lock(path) as third:self.assertTrue(third)


if __name__=='__main__':unittest.main()
