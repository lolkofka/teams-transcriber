"""Verify source launches expose native media APIs before Teams initializes.

No microphone/camera stream is opened and no account is needed.
"""
import threading
import time

import webview

from teams_browser import TeamsBrowser, configure_macos_media

configure_macos_media()
main = webview.create_window('Transcriber media regression', html='<p>Ready</p>')
browser = TeamsBrowser()
result = {}


def check():
    from Foundation import NSURL
    from PyObjCTools import AppHelper
    from webview.platforms.cocoa import BrowserView
    try:
        assert main.events.loaded.wait(10), 'Main window load timed out'
        window = browser.open()
        assert window.events.loaded.wait(20), 'Teams window load timed out'
        finished = threading.Event()

        def load_fixture():
            view = BrowserView.instances[window.uid].webview
            view.loadHTMLString_baseURL_('<p>Media API check</p>',
                                        NSURL.URLWithString_('https://teams.microsoft.com/'))
            finished.set()

        AppHelper.callAfter(load_fixture)
        assert finished.wait(5), 'Fixture dispatch timed out'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if browser.evaluate_js('document.body?.innerText') == 'Media API check':
                break
            time.sleep(.1)
        else:
            raise AssertionError('Fixture did not load')
        assert browser.evaluate_js('isSecureContext'), 'Fixture must be secure'
        assert browser.evaluate_js('typeof navigator.mediaDevices') == 'object', 'MediaDevices unavailable'
        assert browser.evaluate_js('typeof navigator.mediaDevices.enumerateDevices') == 'function', 'Enumeration unavailable'
        assert browser.evaluate_js('typeof navigator.mediaDevices.getUserMedia') == 'function', 'Capture unavailable'
        result['ok'] = True
    except Exception as exc:
        result['error'] = repr(exc)
    finally:
        browser.close()
        main.destroy()


timer = threading.Timer(45, main.destroy)
timer.daemon = True
timer.start()
webview.start(check, private_mode=False)
timer.cancel()
print(result)
assert result.get('ok'), result
