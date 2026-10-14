"""Authenticated, resumable shared archive. Put behind HTTPS or use an SSH tunnel."""
import argparse
import hashlib
import hmac
import html
import io
import json
import os
import re
import secrets
import tarfile
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from shared_archive import ArchiveStore, TEXT_EXTENSIONS

PAGE = '''<!doctype html><meta charset="utf-8"><title>Архив пар</title>
<style>body{font:16px system-ui;max-width:1000px;margin:40px auto;background:#171721;color:#eee}a{color:#c7afff}article{padding:20px;border:1px solid #555;margin:15px 0}pre{white-space:pre-wrap}button,input{font:inherit;padding:8px}</style>
<h1>Архив пар</h1><main id="list">Загрузка…</main><pre id="text"></pre>
<script>const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
fetch('/api/meetings').then(r=>r.json()).then(items=>{
list.innerHTML=items.map(m=>`<article><b>${esc(m.meta.subject||'Пара')}</b> ${esc(m.meta.date)} ${esc(m.meta.time)}<p>Источников: ${m.clients}</p><button data-id="${m.meeting}">Читать текст и чат</button> <a href="/api/meetings/${m.meeting}/transcript.txt" download>TXT</a> · <a href="/api/meetings/${m.meeting}/transcript.json" download>JSON</a> · <a href="/api/meetings/${m.meeting}/full.tar" download>Все материалы без сжатия</a></article>`).join('')||'Пока нет загруженных пар';
list.onclick=async e=>{if(e.target.dataset.id){text.textContent=await(await fetch('/api/meetings/'+e.target.dataset.id+'/transcript.txt')).text();text.scrollIntoView();}};
}).catch(()=>list.textContent='Не удалось загрузить список');</script>'''
LOGIN = '''<!doctype html><meta charset="utf-8"><title>Вход в архив пар</title><h1>Архив пар</h1><form method="post" action="/login"><label>Ключ доступа <input name="token" type="password" required></label><button>Войти</button></form>'''


class ArchiveHTTP(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, root, token, prefix=''):
        if not token or len(token) < 24:
            raise ValueError('Use a token of at least 24 characters')
        self.store = ArchiveStore(root)
        self.token = token
        self.prefix = prefix.rstrip('/')
        self.sessions = {}
        self.tickets = {}
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self, *args):
        pass

    def authorized(self):
        supplied = self.headers.get('X-Token', '')
        if supplied and hmac.compare_digest(supplied, self.server.token):
            return True
        try:
            jar = cookies.SimpleCookie(self.headers.get('Cookie', ''))
            session = jar.get('archive_session')
            return session is not None and self.server.sessions.get(session.value, 0) > time.time()
        except cookies.CookieError:
            return False

    def send(self, code, data, mime='application/json; charset=utf-8', headers=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if code>=400:
            self.send_header('Connection','close');self.close_connection=True
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers()
        self.wfile.write(data)

    def body(self, maximum=16*1024*1024):
        size = int(self.headers.get('Content-Length', '-1'))
        if not 0 <= size <= maximum:
            raise ValueError('Invalid request size')
        body = self.rfile.read(size)
        if len(body) != size: raise ValueError('Incomplete request')
        return body

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            if path == '/login':
                from urllib.parse import parse_qs
                token = parse_qs(self.body(4096).decode()).get('token',[''])[0]
                if not hmac.compare_digest(token, self.server.token):
                    return self.send(401, b'Access denied','text/plain')
                session = secrets.token_urlsafe(32)
                self.server.sessions = {k:v for k,v in self.server.sessions.items() if v>time.time()}
                self.server.sessions[session] = time.time()+43200
                prefix=self.server.prefix
                secure='; Secure' if prefix else ''
                return self.send(303, b'', headers={'Location':prefix+'/', 'Set-Cookie':f'archive_session={session}; HttpOnly; SameSite=Strict; Path={prefix or "/"}; Max-Age=43200{secure}'})
            if not self.authorized(): return self.send(401, {'error':'Unauthorized'})
            if path == '/api/ticket':
                self.body(4096)
                ticket=secrets.token_urlsafe(32)
                self.server.tickets={k:v for k,v in self.server.tickets.items() if v>time.time()}
                self.server.tickets[ticket]=time.time()+60
                return self.send(200, {'ticket':ticket})
            if path == '/api/manifests':
                manifest = json.loads(self.body())
                return self.send(200, self.server.store.commit(manifest))
            if path == '/api/empty':
                sha = json.loads(self.body(4096))['sha256']
                if sha != hashlib.sha256(b'').hexdigest(): raise ValueError('Invalid empty blob')
                self.server.store.blob(sha).touch(exist_ok=True)
                return self.send(200, {'complete':True})
            self.send(404, {})
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400, {'error':str(exc)})

    def do_PUT(self):
        if not self.authorized(): return self.send(401, {'error':'Unauthorized'})
        try:
            sha = urlsplit(self.path).path.removeprefix('/api/blobs/')
            target = self.server.store.blob(sha)
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',self.headers.get('Content-Range',''))
            if not match: raise ValueError('Content-Range required')
            start,end,total = map(int,match.groups())
            data = self.body(8*1024*1024)
            if len(data) != end-start+1 or not 0 <= start <= end < total:
                raise ValueError('Invalid chunk')
            with self.server.store.lock:
                if target.exists(): return self.send(200, {'complete':True,'size':target.stat().st_size})
                partial = target.with_suffix('.part')
                offset = partial.stat().st_size if partial.exists() else 0
                if start != offset:
                    return self.send(409, {'offset':offset})
                with partial.open('ab') as output:
                    output.write(data); output.flush(); os.fsync(output.fileno())
                complete = partial.stat().st_size == total
                if complete:
                    hasher = hashlib.sha256()
                    with partial.open('rb') as source:
                        for chunk in iter(lambda:source.read(1024*1024),b''): hasher.update(chunk)
                    if hasher.hexdigest() != sha:
                        partial.unlink()
                        raise ValueError('SHA256 mismatch')
                    partial.replace(target)
                self.send(200, {'complete':complete,'offset':end+1})
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400, {'error':str(exc)})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/ticket/'):
            ticket=path.rsplit('/',1)[1]
            expiry=self.server.tickets.pop(ticket,0)
            if expiry <= time.time(): return self.send(401,{'error':'Ticket expired'})
            session=secrets.token_urlsafe(32)
            self.server.sessions[session]=time.time()+43200
            prefix=self.server.prefix;secure='; Secure' if prefix else ''
            return self.send(303,b'',headers={'Location':prefix+'/', 'Set-Cookie':f'archive_session={session}; HttpOnly; SameSite=Strict; Path={prefix or "/"}; Max-Age=43200{secure}'})
        if path == '/' and not self.authorized():
            return self.send(200,LOGIN.replace('action="/login"','action="'+self.server.prefix+'/login"').encode(),'text/html; charset=utf-8')
        if not self.authorized(): return self.send(401, {'error':'Unauthorized'})
        store = self.server.store
        try:
            if path == '/':
                page=PAGE.replace("'/api", "'"+self.server.prefix+"/api").replace('"/api','"'+self.server.prefix+'/api')
                return self.send(200,page.encode(),'text/html; charset=utf-8')
            if path == '/api/meetings': return self.send(200,store.index())
            if path.startswith('/api/blob-status/'):
                target = store.blob(path.rsplit('/',1)[1])
                partial = target.with_suffix('.part')
                return self.send(200,dict(complete=target.exists(),offset=target.stat().st_size if target.exists() else partial.stat().st_size if partial.exists() else 0))
            if path.startswith('/api/blobs/'):
                target = store.blob(path.rsplit('/',1)[1])
                self.send_response(200);self.send_header('Content-Type','application/octet-stream');self.send_header('Content-Length',str(target.stat().st_size));self.end_headers()
                with target.open('rb') as source:
                    for chunk in iter(lambda:source.read(1024*1024),b''): self.wfile.write(chunk)
                return
            match = re.fullmatch(r'/api/meetings/([0-9a-f]{64})/(manifest|transcript\.(txt|json|jsonl)|full.tar)',path)
            if not match: return self.send(404,{})
            meeting, action, format = match.groups()
            manifests = store.manifests(meeting)
            if not manifests: return self.send(404,{})
            if action == 'manifest': return self.send(200,manifests)
            if format:
                return self.send(200,store.export(meeting,format),'text/plain; charset=utf-8' if format=='txt' else 'application/json; charset=utf-8',{'Content-Disposition':f'attachment; filename="transcript.{format}"'})
            self.send_response(200);self.send_header('Content-Type','application/x-tar');self.send_header('Content-Disposition',f'attachment; filename="{meeting}.tar"');self.send_header('Connection','close');self.end_headers();self.close_connection=True
            with tarfile.open(fileobj=self.wfile, mode='w|') as archive:
                for format in ('txt','json','jsonl'):
                    data = store.export(meeting,format)
                    info = tarfile.TarInfo('final/transcript.'+format);info.size=len(data);archive.addfile(info,io.BytesIO(data))
                for manifest in manifests:
                    data=json.dumps(manifest,ensure_ascii=False,indent=2).encode();info=tarfile.TarInfo('sources/'+manifest['client']+'/manifest.json');info.size=len(data);archive.addfile(info,io.BytesIO(data))
                    for item in manifest['files']:
                        archive.add(store.blob(item['sha256']), arcname='sources/'+manifest['client']+'/'+item['path'],recursive=False)
        except (ValueError, KeyError, FileNotFoundError):
            self.send(404,{'error':'Not found'})


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--prefix',default='')
    args=parser.parse_args()
    ArchiveHTTP((args.host,args.port),args.root,os.environ.get('ARCHIVE_TOKEN',''),args.prefix).serve_forever()
