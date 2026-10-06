"""Python-owned Teams WebView2 window for Windows desktop automation.

The Teams page does not receive the transcriber's pywebview JS API. Python can
still inspect and control the page through evaluate_js, navigation, and the
underlying CoreWebView2 object when native APIs are needed.
"""

import json
import threading
import time
from urllib.parse import urlparse

import webview


TEAMS_URL = "https://teams.microsoft.com/"


class TeamsBrowser:
    def __init__(self):
        self._window = None
        self._lock = threading.RLock()
        self._popup_handler = None

    def open(self):
        """Open Teams once, or bring its existing window to the foreground."""
        with self._lock:
            if self._window is None:
                window = webview.create_window(
                    "Teams — Teams transcriber", TEAMS_URL, js_api=None,
                    width=1250, height=850, min_size=(820, 600),
                    text_select=True, background_color="#202124",
                )
                if window is None:
                    raise RuntimeError("не удалось создать окно WebView2")
                self._window = window

                def on_closed():
                    with self._lock:
                        if self._window is window:
                            self._window = None
                            self._popup_handler = None

                window.events.closed += on_closed
            else:
                self._window.show()
                self._window.restore()
            return self._window

    def close(self):
        with self._lock:
            window = self._window
            self._window = None
            self._popup_handler = None
        if window is not None:
            window.destroy()

    def is_open(self):
        with self._lock:
            return self._window is not None

    def _require_window(self):
        with self._lock:
            if self._window is None:
                raise RuntimeError("окно Teams не открыто")
            return self._window

    def url(self):
        return self._require_window().get_current_url()

    def navigate(self, url):
        self._require_window().load_url(url)

    def evaluate_js(self, script):
        """Execute JavaScript in the Teams top-level page and return its value."""
        return self._require_window().evaluate_js(script)

    def wait_for(self, selector, timeout=15, interval=0.25):
        """Wait for a DOM element; callers supply selectors for the current Teams UI."""
        script = f"Boolean(document.querySelector({json.dumps(selector)}))"
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.evaluate_js(script):
                return True
            time.sleep(interval)
        return False

    def click(self, selector):
        script = ("(() => { const e = document.querySelector(" + json.dumps(selector)
                  + "); if (!e) return false; e.click(); return true; })()")
        return bool(self.evaluate_js(script))

    def native_handle(self):
        """Window handle for UI Automation (including the Outlook calendar iframe)."""
        return self.with_core_webview2(lambda core: self._require_window().native.Handle.ToInt64())

    def install_meeting_popup_handler(self):
        """Keep Teams meeting links in our controllable WebView2 window."""
        if self._popup_handler is not None:
            return

        def handler(sender, args):
            uri = args.Uri or ""
            parsed = urlparse(uri)
            if (parsed.scheme == "https" and
                    parsed.hostname in ("teams.microsoft.com", "teams.cloud.microsoft")):
                try:
                    sender.Navigate(uri)
                    args.Handled = True
                except Exception:
                    pass

        def attach(core):
            core.NewWindowRequested += handler

        self.with_core_webview2(attach)
        self._popup_handler = handler

    def with_core_webview2(self, callback, timeout=15):
        """Run callback(CoreWebView2) on the native UI thread for advanced automation.

        WebView2 events, navigation, request interception and DevTools protocol
        calls can be configured here. The callback must not block the UI thread.
        """
        window = self._require_window()
        if not window.events.loaded.wait(timeout):
            raise TimeoutError("окно Teams не загрузилось")
        form = window.native
        if form is None:
            raise RuntimeError("нативное окно WebView2 недоступно")
        from System import Action

        result = {}

        def invoke():
            try:
                core = form.webview.CoreWebView2
                if core is None:
                    raise RuntimeError("CoreWebView2 недоступен")
                result["value"] = callback(core)
            except Exception as exc:
                result["error"] = exc

        if form.InvokeRequired:
            form.Invoke(Action(invoke))
        else:
            invoke()
        if "error" in result:
            raise result["error"]
        return result.get("value")


# Shared handle for Python automation modules running in the app process.
BROWSER = TeamsBrowser()
