"""Archive sync runs only while the desktop app is open. No system cleanup job."""
import datetime as dt
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

from shared_archive import TEXT_EXTENSIONS, digest, safe_path


class ArchiveClient:
    def __init__(self, url, token):
        self.url=url.rstrip('/')
        self.token=token
        self.connection=None

    def close(self):
        if self.connection is not None:
            self.connection.close();self.connection=None

    def __del__(self):
        self.close()

    def request(self, path, body=None, method=None, headers=None):
        import http.client
        import io
        from urllib.parse import urlsplit
        from urllib.error import HTTPError
        target=urlsplit(self.url)
        if target.scheme not in ('http','https') or target.username or target.password:
            raise ValueError('Use an HTTP(S) archive address without embedded credentials')
        if self.connection is None:
            factory=http.client.HTTPSConnection if target.scheme=='https' else http.client.HTTPConnection
            self.connection=factory(target.hostname,target.port,timeout=60)
        try:
            self.connection.request(method or ('POST' if body is not None else 'GET'),target.path.rstrip('/')+path,
                                    body=body,headers={'X-Token':self.token,**(headers or {})})
            response=self.connection.getresponse()
            if response.status>=400:
                data=response.read()
                raise HTTPError(self.url+path,response.status,response.reason,response.headers,io.BytesIO(data))
            return response
        except Exception:
            self.connection.close();self.connection=None
            raise

    def json(self,path,body=None):
        with self.request(path,None if body is None else json.dumps(body,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'}) as response:
            return json.load(response)

    def upload(self, path, sha, size):
        import ssl
        from http.client import RemoteDisconnected
        from urllib.error import HTTPError, URLError
        failures=0
        while True:
            try:
                status=self.json('/api/blob-status/'+sha)
                if status['complete']:
                    if status['offset']!=size: raise ValueError('Remote size mismatch')
                    return
                if size==0:
                    self.json('/api/empty',{'sha256':sha})
                    return
                offset=status['offset']
                if not 0 <= offset <= size:raise ValueError('Invalid remote offset')
                with Path(path).open('rb') as source:
                    source.seek(offset)
                    while offset<size:
                        data=source.read(min(512*1024,size-offset))
                        if not data:raise ValueError('Source changed during upload')
                        with self.request('/api/blobs/'+sha,data,'PUT',{'Content-Range':f'bytes {offset}-{offset+len(data)-1}/{size}'}) as response:
                            result=json.load(response)
                        offset+=len(data);failures=0
                if not result.get('complete'):raise ValueError('Upload not verified')
                return
            except (HTTPError,URLError,RemoteDisconnected,TimeoutError,ConnectionError,ssl.SSLError) as exc:
                if isinstance(exc,ssl.SSLCertVerificationError):raise
                if isinstance(exc,HTTPError) and exc.code not in (408,409,429,500,502,503,504):raise
                failures+=1
                if failures>=6:raise
                time.sleep(min(8,2**failures))


from contextlib import contextmanager

@contextmanager
def sync_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell()==0:handle.write(b'0');handle.flush()
        handle.seek(0)
        acquired=False
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            acquired=True
        except OSError:pass
        try:yield acquired
        finally:
            if acquired:
                if os.name=='nt':
                    handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(handle.fileno(),fcntl.LOCK_UN)


def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    os.chmod(temporary,0o600);temporary.replace(path)


def fingerprint(path):
    before=path.stat()
    hasher=hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''): hasher.update(chunk)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise ValueError('File is changing')
    return hasher.hexdigest(),after.st_size


def lesson_meta(folder):
    meta={}
    try:
        meta=json.loads((folder/'lesson.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):pass
    transcript=folder/'transcript.txt'
    if transcript.exists():
        with transcript.open(encoding='utf-8',errors='replace') as source:
            for line in source:
                if not line.startswith('#'): break
                if line.startswith('# Пара: '): meta['subject']=line[8:].strip()
                if line.startswith('# Преподаватель: '): meta['teacher']=line.split(': ',1)[1].strip()
                if line.startswith('# Дата: '):
                    date,_,slot=line[8:].strip().partition(', ');meta.update(date=date,time=slot)
    match=re.fullmatch(r'(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})',folder.name)
    if match:
        meta.setdefault('date',match[1]);meta.setdefault('time',match[2]+':'+match[3])
    meta.setdefault('subject',folder.parent.name)
    meta.setdefault('date',folder.name[:10])
    meta.setdefault('time','')
    # Scheduled start, rather than recording start, groups clients entering late.
    identity=[meta['date'],meta['time'].split('-')[0].strip(),re.sub(r'\W+',' ',meta['subject'].casefold()).strip()]
    return digest(json.dumps(identity,ensure_ascii=False).encode()),meta


def lesson_end(folder,meta):
    try:
        end=meta['time'].split('-')[1].strip()
        return dt.datetime.fromisoformat(meta['date']+'T'+end).timestamp()
    except (KeyError,IndexError,ValueError):
        # No schedule: use the latest original material, so cleanup is never early.
        files=[p for p in folder.rglob('*') if p.is_file() and not p.is_symlink()]
        return max((p.stat().st_mtime for p in files),default=time.time())


class ArchiveSync(threading.Thread):
    def __init__(self, root, state_path, settings, active_folder, on_log):
        super().__init__(daemon=True,name='archive-sync')
        self.root=Path(root)
        self.state_path=Path(state_path)
        self.settings=settings;self.active_folder=active_folder;self.on_log=on_log
        self.stop_event=threading.Event()
        try:self.state=json.loads(self.state_path.read_text())
        except (OSError,ValueError):self.state={}
        self.state.setdefault('client',str(uuid.uuid4()))
        self.state.setdefault('receipts',{})
        self.state.setdefault('revisions',{})
        self._error=None

    def save(self):atomic_json(self.state_path,self.state)

    def sync_folder(self,client,folder,text_only=False):
        if not (folder/'transcript.txt').exists() and not (folder/'lesson.json').exists():return
        meeting,meta=lesson_meta(folder)
        active=self.active_folder()
        active=Path(active).resolve() if active else None
        receipt=self.state['receipts'].get(str(folder),{})
        files={item['path']:item for item in receipt.get('files',[])}
        changed=False
        for path in sorted(folder.rglob('*'),key=lambda p:(p.suffix.lower() not in TEXT_EXTENSIONS,str(p))):
            if self.stop_event.is_set():return
            if not path.is_file() or path.is_symlink() or any(part.startswith('.') for part in path.relative_to(folder).parts):continue
            relative=path.relative_to(folder).as_posix()
            is_text=path.suffix.lower() in TEXT_EXTENSIONS
            if text_only and not is_text:continue
            if not is_text and (active==folder.resolve() or time.time()-path.stat().st_mtime<30):continue
            previous=files.get(relative,{})
            if not is_text and previous.get('size')==path.stat().st_size and previous.get('mtime')==path.stat().st_mtime:
                continue
            try:
                if is_text:
                    # Snapshot small changing text separately; never copy an active audio recording.
                    data=path.read_bytes();sha=digest(data);size=len(data)
                    temporary=self.state_path.parent/('.archive-text-'+sha)
                    temporary.write_bytes(data)
                    upload_path=temporary
                else:
                    sha,size=fingerprint(path);upload_path=path
                item=dict(path=relative,sha256=sha,size=size,mtime=path.stat().st_mtime)
                if files.get(relative,{}).get('sha256')==sha:
                    continue
                self.on_log(f'[archive] загружаю {relative} ({size} байт)')
                client.upload(upload_path,sha,size)
                # Recheck binary contents before acknowledging them locally.
                if not is_text and fingerprint(path)!=(sha,size):continue
                files[relative]=item;changed=True
                # Publish text immediately and acknowledge each completed material independently.
                manifest=dict(meeting=meeting,client=self.state['client'],meta=meta,files=list(files.values()))
                client.json('/api/manifests',manifest)
                self.state['receipts'][str(folder)]=manifest
                self.save();receipt=manifest;changed=False
            finally:
                if is_text and 'temporary' in locals():temporary.unlink(missing_ok=True)
        if changed or not receipt:
            manifest=dict(meeting=meeting,client=self.state['client'],meta=meta,files=list(files.values()))
            client.json('/api/manifests',manifest)
            self.state['receipts'][str(folder)]=manifest
            self.save()
        if not text_only:self.cleanup(folder,meta,files,active)

    def cleanup(self,folder,meta,files,active=None,now=None):
        if self.stop_event.is_set():return
        now=now or time.time()
        if (active and folder.resolve()==active) or now<lesson_end(folder,meta)+3*86400:return
        # Only files present in a committed manifest are eligible. TXT/JSON and all other text stay.
        for relative,item in files.items():
            path=folder.joinpath(*safe_path(relative).parts)
            if path.suffix.lower() in TEXT_EXTENSIONS or not path.is_file() or path.is_symlink():continue
            if fingerprint(path)==(item['sha256'],item['size']):path.unlink()

    def pull(self,client):
        for meeting in client.json('/api/meetings'):
            if self.stop_event.is_set():return
            key=meeting['meeting']
            if not re.fullmatch('[0-9a-f]{64}',key):raise ValueError('Invalid remote meeting')
            if self.state['revisions'].get(key)==meeting['revision']:continue
            folder=self.root/'shared'/key
            folder.mkdir(parents=True,exist_ok=True)
            for format in ('txt','json','jsonl'):
                with client.request('/api/meetings/'+key+'/transcript.'+format) as response:data=response.read()
                temp=folder/('transcript.'+format+'.tmp');temp.write_bytes(data);temp.replace(folder/('transcript.'+format))
            manifests=client.json('/api/meetings/'+key+'/manifest')
            atomic_json(folder/'materials.json',manifests)
            for manifest in manifests:
                owner=manifest['client']
                if not re.fullmatch('[A-Za-z0-9-]{1,100}',owner):raise ValueError('Invalid remote owner')
                for item in manifest['files']:
                    relative=safe_path(item['path'])
                    if relative.suffix.lower() not in TEXT_EXTENSIONS:continue
                    target=folder/'sources'/owner/relative
                    target.parent.mkdir(parents=True,exist_ok=True)
                    if target.exists() and fingerprint(target)[0]==item['sha256']:continue
                    with client.request('/api/blobs/'+item['sha256']) as response:data=response.read()
                    if digest(data)!=item['sha256']:raise ValueError('Download checksum mismatch')
                    temp=target.with_name(target.name+'.tmp');temp.write_bytes(data);temp.replace(target)
            self.state['revisions'][key]=meeting['revision'];self.save()
            self.on_log('[archive] обновлена общая текстовая история: '+meeting['meta'].get('subject',key))

    def once(self):
        cfg=self.settings();url=cfg.get('archive_url','').strip();token=cfg.get('archive_token','').strip()
        if not url or not token:return
        with sync_lock(self.state_path.with_suffix('.lock')) as acquired:
            if not acquired:return
            try:self.state=json.loads(self.state_path.read_text())
            except (OSError,ValueError):pass
            client=ArchiveClient(url,token)
            try:self._once(client)
            finally:client.close()

    def _once(self,client):
        # Authenticate before sending material or considering any cleanup.
        client.json('/api/meetings')
        self.pull(client)  # Missed lessons must arrive before potentially large audio uploads.
        folders=[]
        if self.root.exists():
            for subject in sorted(self.root.iterdir()):
                if not subject.is_dir() or subject.is_symlink() or subject.name in ('shared','camera'):continue
                for folder in sorted(subject.iterdir()):
                    if folder.is_dir() and not folder.is_symlink() and re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{2}-\d{2}',folder.name):
                        folders.append(folder)
        for folder in folders:self.sync_folder(client,folder,text_only=True)
        self.pull(client)
        for folder in folders:self.sync_folder(client,folder)
        self.pull(client)

    def run(self):
        while not self.stop_event.is_set():
            try:self.once();self._error=None
            except Exception as exc:
                error=str(exc)
                if error!=self._error:self.on_log('[archive] синхронизация отложена: '+error);self._error=error
            self.stop_event.wait(30)

    def close(self):
        self.stop_event.set()
        if self.is_alive():self.join(timeout=2)
