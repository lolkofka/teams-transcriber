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
</article></section></div><script>
navigator.mediaDevices.getUserMedia({audio:true}).then(stream => {
 const track=stream.getAudioTracks()[0]; track.enabled=true;
 window.fixtureGuardDenied=!track.enabled && !track.clone().enabled;
});
</script></body></html>''', encoding='utf-8')
original_install = WindowsTeamsChat.install

def install(self, browser):
    from Microsoft.Web.WebView2.Core import CoreWebView2HostResourceAccessKind
    browser.with_core_webview2(lambda core: core.SetVirtualHostNameToFolderMapping(
        'teams.microsoft.com', folder.name, CoreWebView2HostResourceAccessKind.DenyCors))
    fixture_script = """class FixtureTrack {
      constructor(){this.kind='audio';this._enabled=true;}
      get enabled(){return this._enabled;} set enabled(value){this._enabled=!!value;}
      addEventListener(){} clone(){return new FixtureTrack();} stop(){}
    }
    navigator.mediaDevices.getUserMedia=async()=>{const track=new FixtureTrack();return {getAudioTracks:()=>[track]};};"""
    task=browser.with_core_webview2(lambda core:core.AddScriptToExecuteOnDocumentCreatedAsync(fixture_script))
    task.GetAwaiter().GetResult()
    original_install(self, browser)

WindowsTeamsChat.install = install
teams_browser.TEAMS_URL = 'https://teams.microsoft.com/smoke.html'
browser = teams_browser.TeamsBrowser()
host = webview.create_window('Windows fixture host', html='<p>Local test host</p>')
result = {}

def check():
    window = None
    try:
        window = browser.open()
        deadline = time.monotonic()+40
        while time.monotonic() < deadline:
            if browser.dom.chat_messages():
                break
            time.sleep(.2)
        guard = browser.with_core_webview2(lambda core: core.ExecuteScriptAsync(
            'JSON.stringify({guard:!!window.__teamsMicGuard,chat:!!window.__teamsChat,denied:window.fixtureGuardDenied===true,api:Object.keys(window.pywebview?.api||{}).length>0})'))
        state = json.loads(json.loads(str(guard.GetAwaiter().GetResult())))
        assert state == dict(guard=True, chat=True, denied=True, api=False), state
        assert browser.dom.chat_messages()[0]['text'] == 'Windows chat fixture'
        result['ok'] = True
        print('WINDOWS WEBVIEW2 SMOKE OK', flush=True)
    except Exception as error:
        result['error'] = repr(error)
        print('WINDOWS WEBVIEW2 SMOKE FAILED', repr(error), flush=True)
    finally:
        if window is not None:
            window.destroy()
        host.destroy()

webview.start(check, gui='edgechromium', private_mode=True)
folder.cleanup()
assert result.get('ok'), result
