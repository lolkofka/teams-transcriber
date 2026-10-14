"""Python-owned Teams window: WebView2 on Windows, WKWebView on macOS.

The Teams page does not receive the transcriber's pywebview JS API. Python can
still inspect and control the page through evaluate_js, navigation, and the
underlying CoreWebView2 object when native APIs are needed.
"""

import json
import plistlib
import sys
import threading
import time
from urllib.parse import urlparse

import webview


TEAMS_URL = "https://teams.microsoft.com/"


def teams_microphone_origin_allowed(*urls):
    """Only our HTTPS Teams page and its Teams frame may reuse mic permission."""
    if len(urls) != 3:
        return False
    for url in urls:
        try:
            parsed = urlparse(str(url or ""))
            if (parsed.scheme != "https" or parsed.hostname not in
                    ("teams.microsoft.com", "teams.cloud.microsoft") or
                    parsed.port not in (None, 443)):
                return False
        except ValueError:
            return False
    return True


def configure_macos_media():
    """Expose WebKit media APIs when running source without an app Info.plist.

    These descriptions do not grant access: macOS still handles permission
    prompts. Frozen builds carry the same keys in their bundle's Info.plist.
    Call before creating any WKWebView, because WebKit caches this capability.
    """
    if sys.platform != "darwin":
        return
    from Foundation import NSBundle
    info = NSBundle.mainBundle().infoDictionary()
    for key, description in {
        "NSMicrophoneUsageDescription": "Микрофон для звонков Teams и записи голоса",
        "NSCameraUsageDescription": "Камера для звонков Teams и записи видео",
    }.items():
        if not info.get(key):
            info[key] = description


def macos_safari_user_agent():
    """Identify the system WebKit as the installed Safari, including its version."""
    from pathlib import Path
    for app in ("/Applications/Safari.app", "/System/Applications/Safari.app",
                "/System/Volumes/Preboot/Cryptexes/App/System/Applications/Safari.app"):
        try:
            with Path(app, "Contents", "Info.plist").open("rb") as stream:
                version = str(plistlib.load(stream)["CFBundleShortVersionString"])
            return ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    f"Version/{version} Safari/605.1.15")
        except (OSError, KeyError, plistlib.InvalidFileException):
            continue
    raise RuntimeError("Не удалось определить версию системного Safari для Teams")


class TeamsBrowser:
    def __init__(self):
        self._window = None
        self._lock = threading.RLock()
        self._popup_handler = None
        self.audio = None
        self._mac_delegate = None
        self.dom = None
        self._volume = 1.0

    def open(self):
        """Open Teams once, or bring its existing window to the foreground."""
        with self._lock:
            if self._window is None:
                configure_macos_media()
                window = webview.create_window(
                    "Teams — Teams transcriber", None if sys.platform == "win32" else TEAMS_URL,
                    html="<p>Подготовка защищённого окна Teams…</p>" if sys.platform == "win32" else None,
                    js_api=None,
                    width=1250, height=850, min_size=(820, 600),
                    text_select=True, background_color="#202124",
                )
                if window is None:
                    raise RuntimeError("не удалось создать окно WebView2")
                self._window = window
                if sys.platform == "darwin":
                    from teams_audio import TeamsAudioTap
                    self.audio = TeamsAudioTap()
                    from teams_macos import MacTeamsDOM
                    self.dom = MacTeamsDOM(self._volume)
                    window.events.before_show += self._configure_macos
                elif sys.platform == "win32":
                    from teams_windows import WindowsTeamsChat
                    self.dom = WindowsTeamsChat()
                    initialized = threading.Event()

                    def configure_windows():
                        with self._lock:
                            if initialized.is_set():
                                return
                            initialized.set()
                        try:
                            self.dom.install(self)
                            self.install_meeting_popup_handler()
                            window.load_url(TEAMS_URL)
                        except Exception as exc:
                            # Keep Teams unopened if the microphone guard cannot be installed.
                            import html
                            import logging
                            logging.exception("Не удалось подготовить защиту Teams WebView2")
                            window.load_html("<h2>Не удалось подготовить защиту Teams</h2>"
                                             "<p>Обновите Microsoft Edge WebView2 Runtime.</p><pre>"
                                             + html.escape(str(exc)) + "</pre>")
                    window.events.loaded += configure_windows
                    if window.events.loaded.is_set():
                        threading.Thread(target=configure_windows, daemon=True).start()

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

    def _configure_macos(self, window):
        from pathlib import Path
        from paths import RESOURCE_DIR
        import WebKit
        from webview.platforms.cocoa import BrowserView
        view = BrowserView.instances[window.uid].webview
        source = Path(RESOURCE_DIR, "ui", "teams-mic-guard.js").read_text(encoding="utf-8")
        view.configuration().userContentController().addUserScript_(
            WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
                source, WebKit.WKUserScriptInjectionTimeAtDocumentStart, False))
        self.audio.install_macos(window)
        self.dom.install(window)
        view.setCustomUserAgent_(macos_safari_user_agent())
        self._mac_delegate = _mac_teams_delegate()
        view.setUIDelegate_(self._mac_delegate)

    def is_open(self):
        with self._lock:
            return self._window is not None

    def set_volume(self, percent):
        self._volume = max(0.0, min(1.0, float(percent) / 100))
        if self.dom is not None and hasattr(self.dom, "set_volume"):
            self.dom.set_volume(self._volume)

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
        if sys.platform == "darwin":
            return self._evaluate_macos(script)
        return self._require_window().evaluate_js(script)

    def _evaluate_macos(self, script, timeout=15):
        # pywebview wraps scripts in eval(), which Microsoft pages forbid via CSP.
        # WKWebView can execute the source directly without that wrapper.
        window = self._require_window()
        if not window.events.loaded.wait(timeout):
            raise TimeoutError("Страница Teams ещё не загрузилась")
        from PyObjCTools import AppHelper
        from webview.platforms.cocoa import BrowserView
        done, result = threading.Event(), {}

        def complete(value, error):
            result.update(value=value, error=str(error) if error else None)
            done.set()

        def evaluate():
            try:
                view = BrowserView.instances[window.uid].webview
                view.evaluateJavaScript_completionHandler_(script, complete)
            except Exception as exc:
                complete(None, exc)
        AppHelper.callAfter(evaluate)
        if not done.wait(timeout):
            raise TimeoutError("Страница Teams не ответила")
        if result.get("error"):
            raise RuntimeError(result["error"])
        return result.get("value")

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
        if sys.platform == "darwin":
            return  # The WK UIDelegate is attached synchronously before navigation.
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
            core.NewWindowRequested -= self._require_window().native.browser.on_new_window_request
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


_mac_delegate_class = None


def _mac_teams_delegate():
    """Keep meeting popups in the same WKWebView, with the audio tap installed."""
    global _mac_delegate_class
    if _mac_delegate_class is None:
        from webview.platforms.cocoa import BrowserView
        from objc import nil, super as objc_super
        from teams_audio import teams_origin

        class TeamsWKDelegate(BrowserView.BrowserDelegate):
            def webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_(
                    self, view, origin, frame, capture_type, decision_handler):
                import WebKit
                # This is the website-level decision; macOS device permissions
                # still apply. Camera requests retain WebKit's standard prompt.
                def origin_url(value):
                    port = int(value.port())
                    suffix = f":{port}" if port else ""
                    return f"{value.protocol()}://{value.host()}{suffix}"

                allowed = (capture_type == WebKit.WKMediaCaptureTypeMicrophone and
                           teams_microphone_origin_allowed(
                               origin_url(origin), origin_url(frame.securityOrigin()), view.URL()))
                decision_handler(WebKit.WKPermissionDecisionGrant if allowed
                                 else WebKit.WKPermissionDecisionPrompt)

            def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
                    self, view, config, action, features):
                url = action.request().URL()
                if url and url.scheme() == "https" and teams_origin(str(url.host() or "")):
                    view.loadRequest_(action.request())
                    return nil
                return objc_super(TeamsWKDelegate, self).webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
                    view, config, action, features)

        _mac_delegate_class = TeamsWKDelegate
    return _mac_delegate_class.alloc().init()
