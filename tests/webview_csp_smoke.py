"""Mac regression: native evaluation works on Microsoft-style CSP pages."""
import threading
from unittest.mock import patch

import webview
from teams_browser import TeamsBrowser, macos_safari_user_agent

window = webview.create_window('Mac CSP check',html='''
<meta http-equiv="Content-Security-Policy" content="script-src 'none'">
<button id="join">Join</button>''')
browser = TeamsBrowser()
with patch('teams_browser.webview.create_window',return_value=window):
    browser.open()
result = {}


def check():
    try:
        assert window.events.loaded.wait(10)
        assert browser.evaluate_js('navigator.userAgent') == macos_safari_user_agent()
        assert browser.evaluate_js("document.querySelector('#join').textContent") == 'Join'
        browser.evaluate_js("document.querySelector('#join').addEventListener('click', () => document.body.dataset.clicked = 'yes')")
        assert browser.click('#join')
        assert browser.evaluate_js('document.body.dataset.clicked') == 'yes'
        result['ok'] = True
    except Exception as exc:
        result['error'] = repr(exc)
    finally:
        window.destroy()


timer=threading.Timer(20,window.destroy);timer.daemon=True;timer.start()
webview.start(check,private_mode=False);timer.cancel()
print(result)
assert result.get('ok'),result
