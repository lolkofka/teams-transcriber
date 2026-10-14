import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from teams_chat import ChatTranscript, message_timestamp
from teams_macos import MacTeamsDOM
from report import _load, _who


class ChatTests(unittest.TestCase):
    def test_dates_are_not_guessed(self):
        now = dt.datetime(2026, 10, 7, 13, 0)
        self.assertEqual(message_timestamp({'timeLabel':'Сегодня в 12:10.'}, now), now.replace(hour=12, minute=10).timestamp())
        self.assertEqual(message_timestamp({'timeLabel':'4 октября 2026 г. 21:35'}, now), dt.datetime(2026,10,4,21,35).timestamp())
        self.assertIsNone(message_timestamp({'timeLabel':'воскресенье 21:35'}, now))
        self.assertIsNone(message_timestamp({'timeLabel':'Сегодня 25:90'}, now))

    def test_dedup_survives_restart_and_reports_keep_chat_label(self):
        with tempfile.TemporaryDirectory() as folder:
            start = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            asr = SimpleNamespace(txt_path=str(Path(folder,'transcript.txt')),
                jsonl_path=str(Path(folder,'transcript.jsonl')), args=SimpleNamespace(),
                t_session=start.timestamp(), header=lambda:'# Запись начата: fixture\n# ---\n')
            events = []
            capture = ChatTranscript(asr, events.append)
            msg = dict(id='message-42', author='Преподаватель', text='Создайте App.razor\nСсылка: https://example.com', datetime=start.isoformat())
            capture.ingest([msg, msg])
            capture.ingest([dict(msg, text='Изменённый текст')])
            ChatTranscript(asr, events.append).ingest([msg])
            self.assertEqual(len(events), 1)
            records = [json.loads(line) for line in Path(asr.jsonl_path).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]['source'], 'chat')
            self.assertIn('[чат] Преподаватель:', Path(asr.txt_path).read_text(encoding='utf-8'))
            header, items = _load(folder)
            self.assertEqual(items[0]['source'], 'chat')
            self.assertEqual(_who(items[0]['speaker'],items[0]['source']), '[чат] Преподаватель')
            # Different messages with identical text must both be kept.
            capture.ingest([dict(msg,id='message-43')])
            self.assertEqual(len(events),2)

    def test_untrusted_frames_cannot_submit_chat(self):
        dom = MacTeamsDOM()
        body=json.dumps(dict(kind='chat',frame='f',messages=[dict(author='Teacher', text='Task')]))
        dom.receive('outlook.office.com',body,None)
        dom.receive('teams.microsoft.com.attacker.test',body,None)
        self.assertEqual(dom.chat_messages(),[])
        dom.receive('teams.cloud.microsoft',body,None)
        self.assertEqual(dom.chat_messages()[0]['text'],'Task')


if __name__ == '__main__':
    unittest.main()
