"""Shared archive storage and conservative transcript reconciliation. Standard library only."""
import datetime as dt
import difflib
import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

TEXT_EXTENSIONS = {'.txt', '.json', '.jsonl', '.md', '.csv', '.srt', '.vtt', '.log', '.xml', '.html', '.yaml', '.yml'}


def safe_path(value):
    path = PurePosixPath(value)
    if not value or '\\' in value or ':' in value or str(path) != value or path.is_absolute() or any(p in ('..', '.') for p in value.split('/')) or any(ord(c)<32 for c in value):
        raise ValueError('Invalid relative path')
    return path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def words(text):
    return ' '.join(re.findall(r'\w+', text.casefold()))


def reconcile(records):
    """Prefer fuller replicas; keep conflicting alternatives and nonmatching speech."""
    merged = []
    for record in sorted(records, key=lambda r: (r.get('ts', 0), r.get('client', ''))):
        text = str(record.get('text', '')).strip()
        if not text:
            continue
        current = dict(record, text=text)
        norm = words(text)
        source = current.get('source', 'teams')
        match = None
        for previous in reversed(merged):
            delta = abs(float(current['ts']) - float(previous['ts']))
            if delta > 30:
                continue
            if (source == 'chat') != (previous.get('source') == 'chat'):
                continue
            other = words(previous['text'])
            if source == 'chat':
                same = (current.get('chat_key') and current.get('chat_key') == previous.get('chat_key')) or (
                    delta <= 1 and norm == other and current.get('speaker') == previous.get('speaker'))
            else:
                # Never merge successive utterances from the same recording.
                replicas = current.get('client') not in previous['contributors']
                same = replicas and delta <= 4 and (norm == other or (
                    min(len(norm.split()), len(other.split())) >= 4 and
                    (norm in other or other in norm or difflib.SequenceMatcher(None, norm, other).ratio() >= .78)))
            if same:
                match = previous
                break
        if match is None:
            current['contributors'] = [current.pop('client', '')]
            current['alternatives'] = []
            merged.append(current)
        else:
            client = current.get('client', '')
            if client not in match['contributors']:
                match['contributors'].append(client)
            if norm != words(match['text']):
                variant = {k: current.get(k) for k in ('client', 'text', 'speaker', 'ts')}
                if len(text) > len(match['text']):
                    variant = {k: match.get(k) for k in ('text', 'speaker', 'ts')}
                    variant['client'] = match['contributors'][0]
                    match['text'] = text
                if variant not in match['alternatives']:
                    match['alternatives'].append(variant)
    return sorted(merged, key=lambda r: r['ts'])


class ArchiveStore:
    def __init__(self, root):
        self.root = Path(root)
        self.blobs = self.root/'blobs'
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root/'archive.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS uploads (meeting TEXT, client TEXT, manifest TEXT, PRIMARY KEY(meeting,client))')
        self.db.commit()

    def blob(self, sha):
        if not re.fullmatch('[0-9a-f]{64}', sha):
            raise ValueError('Invalid SHA256')
        return self.blobs/sha

    def manifests(self, meeting):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT manifest FROM uploads WHERE meeting=? ORDER BY client', (meeting,))]

    def commit(self, manifest):
        meeting, client = manifest['meeting'], manifest['client']
        if not re.fullmatch('[0-9a-f]{64}', meeting) or not re.fullmatch('[A-Za-z0-9-]{1,100}', client):
            raise ValueError('Invalid identity')
        if not isinstance(manifest.get('files'), list) or len(manifest['files']) > 20000:
            raise ValueError('Invalid manifest')
        seen = set()
        for item in manifest['files']:
            safe_path(item['path'])
            if item['path'] in seen:
                raise ValueError('Duplicate path')
            seen.add(item['path'])
            blob = self.blob(item['sha256'])
            if not blob.exists() or blob.stat().st_size != item['size']:
                raise ValueError('Missing verified blob')
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO uploads VALUES(?,?,?)', (meeting, client, json.dumps(manifest, ensure_ascii=False)))
            self.db.commit()
        return self.summary(meeting)

    def summary(self, meeting):
        manifests = self.manifests(meeting)
        if not manifests:
            raise KeyError(meeting)
        encoded = json.dumps(manifests, sort_keys=True, ensure_ascii=False).encode()
        return dict(meeting=meeting, revision=digest(encoded), meta=manifests[0].get('meta', {}),
                    clients=len(manifests), files=sum(len(m['files']) for m in manifests))

    def index(self):
        with self.lock:
            ids = [r[0] for r in self.db.execute('SELECT DISTINCT meeting FROM uploads')]
        return sorted((self.summary(i) for i in ids), key=lambda i: i['meta'].get('date', ''), reverse=True)

    def transcript(self, meeting):
        records = []
        for manifest in self.manifests(meeting):
            for file in manifest['files']:
                if PurePosixPath(file['path']).name != 'transcript.jsonl':
                    continue
                for line in self.blob(file['sha256']).read_text(encoding='utf-8', errors='replace').splitlines():
                    try:
                        record = json.loads(line)
                        timestamp = float(record['ts'])
                        if not record.get('text') or not 0 < timestamp < 1e11:
                            continue
                        records.append(dict(record, ts=timestamp, client=manifest['client']))
                    except (ValueError, KeyError, TypeError):
                        continue
        return reconcile(records)

    def export(self, meeting, format):
        records = self.transcript(meeting)
        if format == 'json':
            return json.dumps(dict(**self.summary(meeting), records=records), ensure_ascii=False, indent=2).encode()
        if format == 'jsonl':
            return ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in records).encode()
        lines = ['# Общая транскрипция', '# '+json.dumps(self.summary(meeting)['meta'], ensure_ascii=False)]
        for r in records:
            stamp = dt.datetime.fromtimestamp(r['ts'], ZoneInfo('Europe/Moscow')).strftime('%Y-%m-%d %H:%M:%S')
            label = ('[чат] ' if r.get('source') == 'chat' else '')+str(r.get('speaker', ''))
            lines.append(f'[{stamp}] {label}: {r["text"]}')
        return ('\n'.join(lines)+'\n').encode()
