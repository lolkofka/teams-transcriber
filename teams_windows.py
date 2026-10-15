"""WebView2 microphone guard, meeting chat, and DOM controls in trusted frames.

No Python API is exposed to the Teams page. Only bounded chat snapshots from
trusted HTTPS Teams documents are accepted, including child frames. Calendar
controls are also read from the Outlook frame; actions run as JavaScript there.
"""
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from paths import RESOURCE_DIR
from teams_macos import DOMButton, MacTeamsControls


def trusted_controls_source(source):
    try:
        url = urlsplit(str(source))
        host = url.hostname or ''
        return (url.scheme == 'https' and url.port in (None, 443) and
                (host in ('teams.microsoft.com', 'teams.cloud.microsoft',
                          'outlook.office.com', 'outlook.office365.com',
                          'outlook.cloud.microsoft') or host.endswith('.teams.microsoft.com')))
    except ValueError:
        return False


class WindowsTeamsChat:
    def __init__(self):
        self._frames = {}
        self._control_frames = {}
        self._lock = threading.Lock()
        self._handlers = []
        self._browser = None

    def receive(self, source, body, frame_ref=None):
        try:
            url = urlsplit(str(source))
            if not isinstance(body, str) or len(body) > 1500000:
                return
            data = json.loads(body)
            if not isinstance(data, dict):
                return
            if data.get('kind') == 'controls':
                if not trusted_controls_source(source) or frame_ref is None or not isinstance(data.get('controls'), list):
                    return
                key = data.get('frame')
                if not isinstance(key, str) or not 1 <= len(key) <= 100:
                    return
                buttons = []
                for item in data['controls'][:1000]:
                    if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('id'), str):
                        continue
                    checked = item.get('checked')
                    buttons.append(DOMButton(key, item['id'][:100], item['name'][:2000],
                                             str(item.get('automationId', ''))[:200],
                                             checked if isinstance(checked, bool) else None, bool(item.get('disabled'))))
                with self._lock:
                    now = time.monotonic()
                    self._control_frames = {k: v for k, v in self._control_frames.items() if now - v[0] < 3}
                    if key in self._control_frames or len(self._control_frames) < 32:
                        self._control_frames[key] = (now, str(source), frame_ref, buttons)
                return
            if (data.get('kind') != 'chat' or url.scheme != 'https' or url.hostname not in
                    ('teams.microsoft.com', 'teams.cloud.microsoft') or url.port not in (None, 443) or
                    not isinstance(data.get('messages'), list)):
                return
            key = data.get('frame')
            if not isinstance(key, str) or not 1 <= len(key) <= 100:
                return
            messages = []
            for item in data['messages'][:50]:
                if not isinstance(item, dict) or not all(isinstance(item.get(k, ''), str) for k in
                        ('id', 'author', 'text', 'datetime', 'timeLabel')):
                    continue
                messages.append({k: item.get(k, '')[:limit] for k, limit in
                                 (('id', 500), ('author', 500), ('text', 20000), ('datetime', 100), ('timeLabel', 200))})
            with self._lock:
                now = time.monotonic()
                self._frames = {k: v for k, v in self._frames.items() if now-v[0] < 3}
                if key in self._frames or len(self._frames) < 32:
                    self._frames[key] = (now, messages)
        except (ValueError, TypeError):
            return

    def chat_messages(self):
        with self._lock:
            now = time.monotonic()
            return [m for stamp, messages in self._frames.values() if now-stamp < 3 for m in messages]

    def buttons(self):
        with self._lock:
            now = time.monotonic()
            return [button for stamp, _, _, buttons in self._control_frames.values()
                    if now-stamp < 3 for button in buttons if not button.disabled]

    def invoke(self, button, timeout=5):
        with self._lock:
            frame = self._control_frames.get(button.frame)
            if frame is None or time.monotonic() - frame[0] >= 3 or not trusted_controls_source(frame[1]):
                return False
            target = frame[2]
        script = ("(() => { const h = window.__teamsAutomation; return !!(h && h.invoke("
                  + json.dumps(button.id) + "," + json.dumps(button.Name) + ")); })()")
        task = self._browser.with_core_webview2(lambda core: target.ExecuteScriptAsync(script))
        if not task.Wait(int(timeout * 1000)):
            raise TimeoutError('Teams не ответил на действие календаря')
        return json.loads(str(task.Result)) is True

    def install(self, browser):
        self._browser = browser
        def message(sender, args):
            try:
                self.receive(str(args.Source), str(args.TryGetWebMessageAsString()), sender)
            except Exception:
                pass  # Other WebView2 messages are not our chat protocol.

        def frame_created(sender, args):
            frame = args.Frame
            frame.WebMessageReceived += message
            frame.FrameCreated += frame_created
            self._handlers.append((frame, message, frame_created))

        def attach(core):
            # This window has no js_api; replace pywebview's API-message parser.
            browser._require_window().native.webview.WebMessageReceived -= browser._require_window().native.browser.on_script_notify
            core.WebMessageReceived += message
            core.FrameCreated += frame_created
            self._handlers.append((core, message, frame_created))
            source = '\n'.join(Path(RESOURCE_DIR, 'ui', name).read_text(encoding='utf-8') for name in
                               ('teams-mic-guard.js', 'teams-chat.js', 'teams-automation.js'))
            return core.AddScriptToExecuteOnDocumentCreatedAsync(source)

        # Wait off the UI thread; first navigation is held on a local placeholder until
        # WebView2 confirms that the document-start scripts are registered.
        task = browser.with_core_webview2(attach)
        task.GetAwaiter().GetResult()


class WindowsTeamsControls(MacTeamsControls):
    """Use the shared DOM control logic with WebView2 frame JavaScript actions."""
