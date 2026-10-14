"""Real WKWebView: meeting chat -> native bridge -> transcript. No real account used."""
import datetime as dt
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import webview
from teams_browser import TeamsBrowser
from teams_chat import ChatTranscript

stamp=dt.datetime.now().isoformat()
fixture='''<nav role="navigation">Private chats</nav>
<section><h2>Чат собрания</h2><div role="list">
<article data-message-id="real-message-1"><h4>Задание, автор: Преподаватель</h4><span>Преподаватель</span>
<time datetime="STAMP">Сегодня 12:10</time><div data-tid="message-body"><p>Создайте App.razor</p><a href="https://example.com">Документация</a></div></article>
<article id="message-2"><h4 aria-label="Второе задание, автор: Студент">Второе задание</h4>
<div aria-label="Сегодня в 12:10.">12:10</div><div aria-label="Содержимое сообщения: Вот длинное сообщение из чата с точным текстом задания"><p>Вот длинное сообщение из чата с точным текстом задания</p></div></article>
</div></section><section><h2>Другой чат</h2><article><h4>Секрет, автор: Посторонний</h4><time datetime="STAMP">12:10</time><div data-tid="message-body">Не сохранять</div></article></section>'''.replace('STAMP',stamp)
window=webview.create_window('Chat fixture',html='<p>Preparing</p>')
browser=TeamsBrowser()
with patch('teams_browser.webview.create_window',return_value=window):
    browser.open()
result={}


def check():
    try:
        assert window.events.loaded.wait(10)
        from PyObjCTools import AppHelper
        from Foundation import NSURL
        from webview.platforms.cocoa import BrowserView
        window.events.loaded.clear()
        AppHelper.callAfter(lambda: BrowserView.instances[window.uid].webview.loadHTMLString_baseURL_(fixture,NSURL.URLWithString_('https://teams.cloud.microsoft/')))
        assert window.events.loaded.wait(10)
        end=time.monotonic()+6
        while len(browser.dom.chat_messages())<2 and time.monotonic()<end:
            time.sleep(.1)
        messages=browser.dom.chat_messages()
        assert len(messages)==2, messages
        assert messages[0]['author']=='Преподаватель',messages
        assert 'App.razor' in messages[0]['text'],messages
        assert messages[1]['author']=='Студент',messages
        with tempfile.TemporaryDirectory() as folder:
            asr=SimpleNamespace(txt_path=str(Path(folder,'transcript.txt')),jsonl_path=str(Path(folder,'transcript.jsonl')),
                args=SimpleNamespace(),t_session=dt.datetime.now().replace(hour=0,minute=0,second=0,microsecond=0).timestamp(),header=lambda:'# fixture\n')
            events=[]
            capture=ChatTranscript(asr,events.append)
            capture.ingest(messages);capture.ingest(messages)
            assert len(events)==2,events
            assert len(Path(asr.jsonl_path).read_text().splitlines())==2
            assert '[чат] Преподаватель:' in Path(asr.txt_path).read_text()
        # Hiding the meeting panel must clear the snapshot, not capture a personal chat instead.
        browser.evaluate_js("document.querySelector('section').remove(); window.__teamsChat.snapshot(); true")
        end=time.monotonic()+2
        while browser.dom.chat_messages() and time.monotonic()<end:
            time.sleep(.1)
        assert not browser.dom.chat_messages()
        # The real transcript UI must display chat separately and preserve overlapping audio drafts.
        window.events.loaded.clear()
        ui=Path('ui/index.html').read_text(encoding='utf-8')
        AppHelper.callAfter(lambda: BrowserView.instances[window.uid].webview.loadHTMLString_baseURL_(ui,NSURL.URLWithString_('https://localhost/')))
        assert window.events.loaded.wait(10)
        event=dict(kind='chat',t0=time.time(),t1=time.time(),speaker='Преподаватель',source='chat',text='Задание App.razor')
        browser.evaluate_js("window.__evt("+json.dumps(dict(kind='draft',t0=event['t0'],t1=event['t1']+1,text='Устная реплика'),ensure_ascii=False)+"); window.__evt("+json.dumps(event,ensure_ascii=False)+"); true")
        end=time.monotonic()+3
        while time.monotonic()<end:
            state=browser.evaluate_js("({text:document.querySelector('.item.final')?.innerText,draft:!!document.querySelector('.item.draft')})")
            if state.get('text'): break
            time.sleep(.1)
        assert 'Чат' in state['text'] and 'Преподаватель' in state['text'] and state['draft'],state
        result.update(ok=True,chat_messages=2,no_duplicates=True,personal_chat_excluded=True)
    except Exception as exc:
        result['error']=repr(exc)
    finally:
        window.destroy()


timer=threading.Timer(35,window.destroy);timer.daemon=True;timer.start()
webview.start(check,private_mode=True)
timer.cancel()
print(result)
assert result.get('ok'),result
