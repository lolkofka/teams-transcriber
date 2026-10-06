"""Optional Windows smoke test for the WebView2 popup event subscription."""

import threading

import webview

from teams_browser import BROWSER
from teams_auto import TeamsControls


window = webview.create_window("Popup smoke", html="<html><body><button>Test</button></body></html>", hidden=True)
BROWSER._window = window
result = {}


def check():
    try:
        BROWSER.install_meeting_popup_handler()
        controls = TeamsControls(BROWSER)
        controls.buttons()
        result["ok"] = BROWSER._popup_handler is not None and BROWSER.native_handle() > 0
    except Exception as exc:
        result["error"] = repr(exc)
    finally:
        window.destroy()


timer = threading.Timer(20, window.destroy)
timer.daemon = True
timer.start()
webview.start(check, gui="edgechromium", private_mode=True)
timer.cancel()
if not result.get("ok"):
    raise RuntimeError(result.get("error", "WebView2 did not finish"))
print("WebView2 popup handler attached")
