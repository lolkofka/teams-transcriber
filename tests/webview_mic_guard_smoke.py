"""Real WKWebView mic guard check, using oscillator tracks (no microphone permission)."""
import threading
import time
from unittest.mock import patch

import webview
from teams_browser import TeamsBrowser

fixture='''<button id="microphone-button" data-tid="microphone-button" aria-label="Mute mic" onclick="this.setAttribute('aria-label',this.getAttribute('aria-label')==='Mute mic'?'Unmute mic':'Mute mic')">Mic</button><button data-tid="camera-button" aria-label="Turn camera on">Camera</button>'''
window=webview.create_window('Mic protection fixture',html='<p>Preparing</p>')
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
        # Replace getUserMedia BEFORE guard injection, to use local synthesized audio only.
        import WebKit
        view=BrowserView.instances[window.uid].webview
        fake='''navigator.mediaDevices.getUserMedia = async () => {
            const ctx=new AudioContext(); window.fixtureContext=ctx;
            const osc=ctx.createOscillator(), dst=ctx.createMediaStreamDestination();osc.connect(dst);osc.start();
            return dst.stream;
        };'''
        guard=open('ui/teams-mic-guard.js').read()
        AppHelper.callAfter(lambda: view.configuration().userContentController().addUserScript_(
            WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(fake+'\n'+guard,WebKit.WKUserScriptInjectionTimeAtDocumentStart,False)))
        # Existing production guard would run before fake; remove scripts and reinstall in fixture order.
        def load():
            controller=view.configuration().userContentController()
            controller.removeAllUserScripts()
            controller.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(fake+'\n'+guard,WebKit.WKUserScriptInjectionTimeAtDocumentStart,False))
            view.loadHTMLString_baseURL_(fixture,NSURL.URLWithString_('https://teams.microsoft.com/'))
        window.events.loaded.clear();AppHelper.callAfter(load)
        assert window.events.loaded.wait(10)
        def wait_for(script):
            end=time.monotonic()+4
            while time.monotonic()<end:
                if browser.evaluate_js(script): return
                time.sleep(.1)
            raise AssertionError(script)
        wait_for("document.getElementById('microphone-button').getAttribute('aria-label') === 'Unmute mic'")
        browser.evaluate_js("navigator.mediaDevices.getUserMedia({audio:true}).then(s=>{window.fixtureTrack=s.getAudioTracks()[0];fixtureTrack.enabled=true;window.fixtureClone=fixtureTrack.clone();fixtureClone.enabled=true;}); true")
        wait_for("Boolean(window.fixtureClone)")
        assert browser.evaluate_js('!fixtureTrack.enabled && !fixtureClone.enabled')
        browser.evaluate_js("document.getElementById('microphone-button').setAttribute('aria-label','Mute mic'); window.dispatchEvent(new Event('offline')); true")
        wait_for("document.getElementById('microphone-button').getAttribute('aria-label') === 'Unmute mic'")
        assert browser.evaluate_js('!fixtureTrack.enabled')
        # A synthetic click (Teams automation) cannot authorize microphone transmission.
        browser.evaluate_js("document.getElementById('microphone-button').click(); true")
        wait_for("document.getElementById('microphone-button').getAttribute('aria-label') === 'Unmute mic'")
        assert browser.evaluate_js("document.querySelector('[data-tid=\"camera-button\"]').getAttribute('aria-label')==='Turn camera on'")
        # Russian labels, as used by this user's Teams account.
        browser.evaluate_js("document.getElementById('microphone-button').setAttribute('aria-label','Выключить микрофон'); true")
        wait_for("document.getElementById('microphone-button').getAttribute('aria-label') === 'Unmute mic'")
        result.update(ok=True,initial_mute=True,blocked_tracks_and_clones=True,offline_mute=True,programmatic_unmute_blocked=True)
    except Exception as exc:
        result['error']=repr(exc)
    finally:
        window.destroy()


timer=threading.Timer(35,window.destroy);timer.daemon=True;timer.start()
webview.start(check,private_mode=True)
timer.cancel()
print(result)
assert result.get('ok'),result
