"""WKWebView playback volume: Web Audio, HTML media and the real header slider."""
import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import webview
from teams_browser import TeamsBrowser

window=webview.create_window('Volume fixture',html='<p>Preparing</p>')
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
        view=BrowserView.instances[window.uid].webview
        def load(html,url):
            window.events.loaded.clear()
            AppHelper.callAfter(lambda:view.loadHTMLString_baseURL_(html,NSURL.URLWithString_(url)))
            assert window.events.loaded.wait(10)
        load('<audio></audio><button>Fixture</button>','https://teams.microsoft.com/')
        browser.evaluate_js('''window.ctx=new AudioContext();window.gain=ctx.createGain();gain.connect(ctx.destination);
          window.detached=new Audio();detached.volume=.8;
          window.capture=new AudioContext();capture.__transcriberCapture=true;
          window.captureGain=capture.createGain();captureGain.connect(capture.destination);true''')
        browser.evaluate_js('window.__teamsPlaybackVolume.setVolume(0);true')
        state=browser.evaluate_js('({status:__teamsPlaybackVolume.status(),element:document.querySelector("audio").volume,detached:detached.volume})')
        assert state['status']['gains']==[0] and state['status']['contexts']==1,state
        assert state['element']==0 and state['detached']==0,state
        browser.evaluate_js('document.querySelector("audio").volume=1;true')
        assert browser.evaluate_js('document.querySelector("audio").volume')==0
        browser.evaluate_js('window.__teamsPlaybackVolume.setVolume(.5);true')
        assert browser.evaluate_js('detached.volume')==.4
        browser.evaluate_js('gain.disconnect(ctx.destination);gain.connect(ctx.destination);true')
        load(Path('ui/index.html').read_text(),'https://localhost/')
        browser.evaluate_js('''window.testVolumes=[];window.pywebview={api:{set_volume:v=>testVolumes.push(v),save_settings:async()=>true}};
          document.getElementById('topVol').value=0;document.getElementById('topVol').dispatchEvent(new Event('input'));true''')
        state=browser.evaluate_js('({label:document.getElementById("topVolv").textContent,setting:document.getElementById("vol").value,values:testVolumes})')
        assert state==dict(label='0%',setting='0',values=[0]),state
        result.update(ok=True,html_media_mute=True,web_audio_mute=True,header_slider=True,capture_excluded=True)
    except Exception as exc:
        result['error']=repr(exc)
    finally:
        window.destroy()


timer=threading.Timer(30,window.destroy);timer.daemon=True;timer.start()
webview.start(check,private_mode=True)
timer.cancel()
print(result)
assert result.get('ok'),result
