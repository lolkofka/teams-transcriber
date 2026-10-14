"""Persist meeting chat alongside speech, without feeding it to speech automation."""
import datetime as dt
import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path

# Shared with the ASR writer, including header creation before models are ready.
TRANSCRIPT_LOCK = threading.RLock()
_MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля',
           'августа', 'сентября', 'октября', 'ноября', 'декабря']


def message_timestamp(message, now=None):
    now = now or dt.datetime.now()
    iso = message.get('datetime', '')
    if iso:
        try:
            value = dt.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()
            return value if math.isfinite(value) else None
        except (ValueError, TypeError, OverflowError):
            pass
    label = message.get('timeLabel', '')
    clock = re.search(r'\b(\d{1,2}):(\d{2})\b', label)
    if not clock:
        return None
    date = now.date() if re.search(r'сегодня|today', label, re.I) else None
    if re.search(r'вчера|yesterday', label, re.I):
        date = now.date() - dt.timedelta(days=1)
    full = re.search(r'(\d{1,2})\s+(' + '|'.join(_MONTHS) + r')\s+(\d{4})', label, re.I)
    try:
        if full:
            date = dt.date(int(full[3]), _MONTHS.index(full[2].lower()) + 1, int(full[1]))
        if date is None:  # Never guess the date of an older, virtualized message.
            return None
        return dt.datetime.combine(date, dt.time(int(clock[1]), int(clock[2]))).timestamp()
    except ValueError:
        return None


class ChatTranscript:
    def __init__(self, asr, on_message):
        self.asr = asr
        self.on_message = on_message
        self.seen = set()
        self.minimum = asr.t_session
        meta = getattr(asr.args, 'meta', None) or {}
        try:
            self.minimum = dt.datetime.fromisoformat(meta['date'] + 'T' + meta['time'].split('-')[0]).timestamp()
        except (KeyError, ValueError):
            pass
        path = Path(asr.jsonl_path)
        if path.exists():
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    record = json.loads(line)
                    if record.get('source') == 'chat' and record.get('chat_key'):
                        self.seen.add(record['chat_key'])
                except ValueError:
                    continue

    def ingest(self, messages):
        for message in messages:
            author = message.get('author', '').strip()
            text = message.get('text', '').strip()
            ts = message_timestamp(message)
            if not author or not text or ts is None or ts < self.minimum or ts > time.time() + 60:
                continue
            # Native IDs stay stable across DOM recycling; fallback uses content and original time.
            identity = message.get('id') or json.dumps([author, ts, text], ensure_ascii=False)
            key = hashlib.sha256(identity.encode()).hexdigest()
            if key in self.seen:
                continue
            wall = dt.datetime.fromtimestamp(ts).strftime('%H:%M:%S')
            record = dict(ts=ts, wall=wall, start=round(ts-self.asr.t_session, 2),
                          end=round(ts-self.asr.t_session, 2), speaker=author, source='chat',
                          text=text, chat_key=key, message_id=message.get('id', ''))
            with TRANSCRIPT_LOCK:
                path = Path(self.asr.txt_path)
                fresh = not path.exists() or path.stat().st_size == 0
                with path.open('a', encoding='utf-8') as txt, Path(self.asr.jsonl_path).open('a', encoding='utf-8') as js:
                    if fresh:
                        txt.write(self.asr.header())
                    txt.write(f'[{wall}] [чат] {author}: {text}\n')
                    js.write(json.dumps(record, ensure_ascii=False) + '\n')
                self.seen.add(key)
            self.on_message(dict(t0=ts, t1=ts, speaker=author, source='chat', text=text))
