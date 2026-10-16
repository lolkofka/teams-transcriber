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
<button data-tid="microphone-button" aria-label="Unmute mic"
 onclick="this.setAttribute('aria-label',this.getAttribute('aria-label')==='Unmute mic'?'Mute mic':'Unmute mic')">Mic</button>
<div><h2>Meeting chat</h2><section><article data-message-id="fixture-message">
<h4 aria-label="Message by Teacher">Message by Teacher</h4>
<div data-tid="message-body">Windows chat fixture</div>
<time datetime="2026-10-14T10:00:00Z">Today 10:00</time>
</article></section></div><script>
navigator.mediaDevices.getUserMedia({audio:true}).then(stream => {
 const track=stream.getAudioTracks()[0]; track.enabled=true;
 window.fixtureTrack=track; window.fixtureClone=track.clone();
 window.fixtureGuardDenied=!track.enabled && !window.fixtureClone.enabled;
});
navigator.permissions.query({name:'microphone'}).then(permission => {
 window.fixtureMicPermission=permission.state;
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
        def evaluate(script):
            task = browser.with_core_webview2(lambda core: core.ExecuteScriptAsync(script))
            return json.loads(str(task.GetAwaiter().GetResult()))

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and evaluate('window.fixtureMicPermission') != 'granted':
            time.sleep(.05)
        assert evaluate('window.fixtureMicPermission') == 'granted', 'Teams microphone permission was not granted'

        button = "document.querySelector('[data-tid=\"microphone-button\"]')"
        evaluate(button + '.click()')  # Automation must not activate the microphone.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if evaluate(button + '.getAttribute("aria-label")') == 'Unmute mic':
                break
            time.sleep(.05)
        assert evaluate('!fixtureTrack.enabled && !fixtureClone.enabled')

        rect = evaluate('JSON.stringify((()=>{const r=' + button +
                        '.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})())')
        rect = json.loads(rect)
        for kind, buttons in [('mousePressed', 1), ('mouseReleased', 0)]:
            args = json.dumps(dict(type=kind, x=rect['x'], y=rect['y'],
                                   button='left', buttons=buttons, clickCount=1))
            task = browser.with_core_webview2(lambda core: core.CallDevToolsProtocolMethodAsync(
                'Input.dispatchMouseEvent', args))
            task.GetAwaiter().GetResult()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if evaluate('fixtureTrack.enabled && fixtureClone.enabled'):
                break
            time.sleep(.05)
        assert evaluate(button + '.getAttribute("aria-label")') == 'Mute mic'
        assert evaluate('fixtureTrack.enabled && fixtureClone.enabled'), 'Manual unmute left tracks disabled'

        evaluate(button + '.click()')
        assert evaluate('!fixtureTrack.enabled && !fixtureClone.enabled'), 'Mute did not disable tracks'
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
