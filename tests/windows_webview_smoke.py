"""Exercise the real WebView2 document-start bridge on a local HTTPS fixture.

The virtual-host mapping serves a fixture locally; no Teams account is used.
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webview
import teams_browser
from teams_windows import WindowsTeamsChat

folder = tempfile.TemporaryDirectory()
Path(folder.name, 'smoke.html').write_text('''<!doctype html><html><body>
<div><h2>Meeting chat</h2><section><article data-message-id="fixture-message">
<h4 aria-label="Message by Teacher">Message by Teacher</h4>
<div data-tid="message-body">Windows chat fixture</div>
<time datetime="2026-10-14T10:00:00Z">Today 10:00</time>
</article></section></div></body></html>''', encoding='utf-8')
original_install = WindowsTeamsChat.install

def install(self, browser):
    from Microsoft.Web.WebView2.Core import CoreWebView2HostResourceAccessKind
    browser.with_core_webview2(lambda core: core.SetVirtualHostNameToFolderMapping(
        'teams.microsoft.com', folder.name, CoreWebView2HostResourceAccessKind.DenyCors))
    original_install(self, browser)

WindowsTeamsChat.install = install
teams_browser.TEAMS_URL = 'https://teams.microsoft.com/smoke.html'
browser = teams_browser.TeamsBrowser()
window = browser.open()
result = {}

def check():
    try:
        deadline = time.monotonic()+40
        while time.monotonic() < deadline:
            if browser.dom.chat_messages():
                break
            time.sleep(.2)
        guard = browser.with_core_webview2(lambda core: core.ExecuteScriptAsync(
            'JSON.stringify({guard:!!window.__teamsMicGuard,chat:!!window.__teamsChat,api:Object.keys(window.pywebview?.api||{}).length>0})'))
        state = json.loads(json.loads(str(guard.GetAwaiter().GetResult())))
        assert state == dict(guard=True, chat=True, api=False), state
        assert browser.dom.chat_messages()[0]['text'] == 'Windows chat fixture'
        result['ok'] = True
        print('WINDOWS WEBVIEW2 SMOKE OK', flush=True)
    except Exception as error:
        result['error'] = repr(error)
        print('WINDOWS WEBVIEW2 SMOKE FAILED', repr(error), flush=True)
    finally:
        window.destroy()

webview.start(check, gui='edgechromium', private_mode=True)
folder.cleanup()
assert result.get('ok'), result
