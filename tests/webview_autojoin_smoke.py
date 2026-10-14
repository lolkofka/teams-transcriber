"""Local Mac check: calendar iframe -> prejoin -> muted devices -> call.

PYTHONPATH=. .venv/bin/python tests/webview_autojoin_smoke.py
No real meeting, account, microphone or camera is used.
"""
import datetime as dt
import html
import json
import threading
import time
from unittest.mock import patch

import webview
from teams_browser import TeamsBrowser
from teams_auto import TeamsMeetingAutomation, match_lesson
from teams_macos import MacTeamsControls

now = dt.datetime.now().replace(second=0, microsecond=0)
lesson = {'subject':'Local fixture', 'start':now,'end':now+dt.timedelta(minutes=80)}
event_name = f"Local fixture, {now:%H:%M} to {lesson['end']:%H:%M}, {now:%B %d, %Y}, Microsoft Teams Meeting"
frame = '<button aria-label="'+html.escape(event_name,quote=True)+'" onclick="parent.openCard()">Local meeting</button>'
document = r"""<div id="stage"><button>Календарь</button><iframe srcdoc="FRAME"></iframe></div>
<script>
window.openCard = () => stage.innerHTML = '<button data-testid="primaryActionButton" onclick="prejoin()">Join</button>';
window.prejoin = () => stage.innerHTML = '<button data-tid="microphone-button" aria-label="Microphone" aria-pressed="true" onclick="this.setAttribute(\'aria-pressed\',\'false\')">Mic</button><button data-tid="camera-button" aria-label="Camera" aria-pressed="true" onclick="this.setAttribute(\'aria-pressed\',\'false\')">Camera</button><button onclick="enter()">Join now</button>';
window.enter = () => {
  if (document.querySelector('[data-tid="microphone-button"]').getAttribute('aria-pressed') !== 'false' ||
      document.querySelector('[data-tid="camera-button"]').getAttribute('aria-pressed') !== 'false') throw new Error('Joined with active device');
  window.enteredMuted = true; stage.innerHTML = '<button data-tid="hangup-button">Leave</button><button>Participants 2</button>';
};
</script>""".replace('FRAME',html.escape(frame,quote=True))

window = webview.create_window('Mac autojoin fixture',html='<p>Preparing</p>')
browser = TeamsBrowser()
with patch('teams_browser.webview.create_window',return_value=window):
    browser.open()
result, logs = {}, []


def check():
    try:
        assert window.events.loaded.wait(10)
        from PyObjCTools import AppHelper
        from Foundation import NSURL
        from webview.platforms.cocoa import BrowserView
        window.events.loaded.clear()
        AppHelper.callAfter(lambda: BrowserView.instances[window.uid].webview.loadHTMLString_baseURL_(
            document,NSURL.URLWithString_('https://teams.microsoft.com/')))
        assert window.events.loaded.wait(10)
        controls = MacTeamsControls(browser)
        deadline=time.monotonic()+5
        while not controls.events() and time.monotonic()<deadline:
            time.sleep(.1)
        matched=match_lesson(lesson,controls.events())
        assert matched, [b.Name for b in controls.buttons()]
        auto=TeamsMeetingAutomation(browser,lambda:{'teams_auto_join':True,'teams_auto_leave':False},lambda _:[lesson],logs.append)
        auto._controls=controls
        auto._opened=True
        auto._tick()
        assert auto._active is lesson, logs
        assert controls.in_call()
        assert controls.participant_count() == 2
        assert window.evaluate_js('Boolean(window.enteredMuted)')
        assert len(browser.dom._frames)>=1
        result.update(ok=True,logs=logs,calendar_iframe=True,muted_devices=True)
    except Exception as exc:
        result['error']=repr(exc)
    finally:
        window.destroy()


timer=threading.Timer(40,window.destroy);timer.daemon=True;timer.start()
webview.start(check,private_mode=False)
timer.cancel()
print(result)
assert result.get('ok'),result
