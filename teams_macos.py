"""macOS calendar/call adapter, including the cross-origin Outlook iframe."""
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from paths import RESOURCE_DIR


def trusted_origin(host):
    from teams_audio import teams_origin
    return teams_origin(host) or host in ("outlook.office.com", "outlook.office365.com", "outlook.cloud.microsoft")


@dataclass
class DOMButton:
    frame: str
    id: str
    Name: str
    AutomationId: str
    checked: object = None
    disabled: bool = False


class MacTeamsDOM:
    def __init__(self, volume=1.0):
        self._frames = {}
        self._chat_frames = {}
        self._lock = threading.Lock()
        self._view = self._handler = None
        self.error = None
        self.volume = volume
        self._volume_applied = {}

    def install(self, window):
        import WebKit
        from webview.platforms.cocoa import BrowserView
        self._view = BrowserView.instances[window.uid].webview
        controller = self._view.configuration().userContentController()
        self._handler = _handler(self)
        controller.addScriptMessageHandler_name_(self._handler, "teamsAutomation")
        source = "window.__transcriberInitialVolume=" + json.dumps(self.volume) + ";\n" + Path(RESOURCE_DIR, "ui", "teams-volume.js").read_text(encoding="utf-8")
        controller.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
            source, WebKit.WKUserScriptInjectionTimeAtDocumentStart, False))
        source = Path(RESOURCE_DIR, "ui", "teams-automation.js").read_text(encoding="utf-8")
        controller.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
            source, WebKit.WKUserScriptInjectionTimeAtDocumentStart, False))
        source = Path(RESOURCE_DIR, "ui", "teams-chat.js").read_text(encoding="utf-8")
        controller.addUserScript_(WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
            source, WebKit.WKUserScriptInjectionTimeAtDocumentStart, False))

    def receive(self, host, body, frame_info):
        if not trusted_origin(host) or not isinstance(body, str) or len(body) > 1500000:
            return
        try:
            data = json.loads(body)
            if isinstance(data, dict) and data.get("kind") == "chat":
                from teams_audio import teams_origin
                if not teams_origin(host) or not isinstance(data.get("messages"), list):
                    return
                key = data.get("frame")
                if not isinstance(key, str) or not 1 <= len(key) <= 100:
                    return
                messages = []
                for item in data["messages"][:50]:
                    if not isinstance(item, dict) or not all(isinstance(item.get(k, ""), str) for k in
                            ("id", "author", "text", "datetime", "timeLabel")):
                        continue
                    messages.append({k: item.get(k, "")[:limit] for k, limit in
                                     (("id", 500), ("author", 500), ("text", 20000), ("datetime", 100), ("timeLabel", 200))})
                with self._lock:
                    now = time.monotonic()
                    self._chat_frames = {k: v for k, v in self._chat_frames.items() if now - v[0] < 3}
                    if len(self._chat_frames) < 32 or key in self._chat_frames:
                        self._chat_frames[key] = (now, messages)
                return
            if not isinstance(data, dict) or not isinstance(data.get("controls"), list):
                return
            key = data["frame"]
            if not isinstance(key, str) or not 1 <= len(key) <= 100:
                return
            buttons = []
            for item in data["controls"][:1000]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("id"), str):
                    continue
                checked = item.get("checked")
                buttons.append(DOMButton(key, item["id"][:100], item["name"][:2000], str(item.get("automationId", ""))[:200],
                                         checked if isinstance(checked, bool) else None, bool(item.get("disabled"))))
            with self._lock:
                now = time.monotonic()
                self._frames = {k: v for k, v in self._frames.items() if now - v[0] < 3}
                if len(self._frames) < 32 or key in self._frames:
                    self._frames[key] = (now, frame_info, buttons)
                from teams_audio import teams_origin
                needs_volume = teams_origin(host) and self._view is not None and self._volume_applied.get(key) != self.volume
            if needs_volume:
                self._apply_volume(key, frame_info)
        except (ValueError, TypeError, KeyError):
            return

    def buttons(self):
        with self._lock:
            now = time.monotonic()
            return [b for stamp, _, buttons in self._frames.values() if now - stamp < 3 for b in buttons if not b.disabled]

    def chat_messages(self):
        with self._lock:
            now = time.monotonic()
            return [m for stamp, messages in self._chat_frames.values() if now - stamp < 3 for m in messages]

    def set_volume(self, volume):
        with self._lock:
            self.volume = volume
            frames = [(key, info) for key, (_, info, _) in self._frames.items()]
        for key, info in frames:
            self._apply_volume(key, info)

    def _apply_volume(self, key, frame_info):
        from PyObjCTools import AppHelper
        import WebKit
        value = self.volume
        script = "window.__teamsPlaybackVolume && window.__teamsPlaybackVolume.setVolume(" + json.dumps(value) + ")"
        def complete(result, error):
            if result and error is None:
                with self._lock:
                    self._volume_applied[key] = value
        def apply():
            try:
                self._view.evaluateJavaScript_inFrame_inContentWorld_completionHandler_(
                    script, frame_info, WebKit.WKContentWorld.pageWorld(), complete)
            except Exception:
                pass
        AppHelper.callAfter(apply)

    def invoke(self, button, timeout=5):
        with self._lock:
            frame = self._frames.get(button.frame)
            if frame is None or time.monotonic() - frame[0] >= 3:
                return False
            frame_info = frame[1]
        script = "window.__teamsAutomation && window.__teamsAutomation.invoke(" + json.dumps(button.id) + "," + json.dumps(button.Name) + ")"
        done, result = threading.Event(), {}
        from PyObjCTools import AppHelper
        import WebKit

        def complete(value, error):
            result.update(value=bool(value), error=str(error) if error else None)
            done.set()

        def evaluate():
            try:
                self._view.evaluateJavaScript_inFrame_inContentWorld_completionHandler_(
                    script, frame_info, WebKit.WKContentWorld.pageWorld(), complete)
            except Exception as exc:
                complete(False, exc)
        AppHelper.callAfter(evaluate)
        if not done.wait(timeout):
            raise TimeoutError("Teams не ответил на действие календаря")
        if result.get("error"):
            raise RuntimeError("Страница Teams изменилась: " + result["error"])
        return result.get("value", False)


_handler_class = None


def _handler(adapter):
    global _handler_class
    if _handler_class is None:
        from Foundation import NSObject

        class TeamsAutomationMessageHandler(NSObject):
            def userContentController_didReceiveScriptMessage_(self, controller, message):
                frame = message.frameInfo()
                origin = frame.securityOrigin()
                if origin.protocol() == "https":
                    self.adapter.receive(str(origin.host()), message.body(), frame)
        _handler_class = TeamsAutomationMessageHandler
    handler = _handler_class.alloc().init()
    handler.adapter = adapter
    return handler


class MacTeamsControls:
    def __init__(self, browser):
        self.browser = browser

    def buttons(self):
        return self.browser.dom.buttons() if self.browser.dom else []

    @staticmethod
    def _name(button):
        return button.Name or ""

    def find(self, predicate, buttons=None):
        return [b for b in (buttons if buttons is not None else self.buttons()) if predicate(b.Name, b)]

    def one(self, predicate, buttons=None):
        found = self.find(predicate, buttons)
        return found[0] if len(found) == 1 else None

    def invoke(self, button):
        return bool(button and self.browser.dom.invoke(button))

    def events(self):
        from teams_auto import parse_calendar_event
        return [(event, button) for button in self.buttons() if (event := parse_calendar_event(button.Name)) is not None]

    def in_call(self, buttons=None):
        from teams_auto import LEAVE_CONTROL
        return bool(self.find(lambda n, b: b.AutomationId == "hangup-button" or LEAVE_CONTROL.match(n.strip()), buttons))

    def device_state(self, kind, buttons=None):
        from teams_auto import DEVICE_OFF_ACTION, DEVICE_ON_ACTION, DEVICE_OFF_STATE, DEVICE_ON_STATE
        ids = {"mic": ("microphone-button", "mic-button", "toggle-mute"),
               "camera": ("camera-button", "video-button", "toggle-video")}[kind]
        words = ("микрофон", "microphone", "mic") if kind == "mic" else ("камера", "camera", "video")
        buttons = buttons if buttons is not None else self.buttons()
        toggles = self.find(lambda n, b: b.AutomationId in ids, buttons)
        button = (toggles[0] if len(toggles) == 1 else None) if toggles else self.one(
            lambda n, b: any(w in n.lower() for w in words), buttons)
        if button is None:
            return None, None
        name = button.Name
        if DEVICE_OFF_ACTION.search(name):
            return False, button
        if DEVICE_ON_ACTION.search(name):
            return True, button
        if name.lower().startswith("turn "):
            return None, button
        if DEVICE_OFF_STATE.search(name) and not DEVICE_ON_STATE.search(name):
            return False, button
        if DEVICE_ON_STATE.search(name) and not DEVICE_OFF_STATE.search(name):
            return True, button
        return button.checked, button

    def participant_count(self, buttons=None):
        # Same semantic labels as the Windows adapter, without Windows imports.
        from teams_auto import TeamsControls
        return TeamsControls.participant_count(self, buttons)
