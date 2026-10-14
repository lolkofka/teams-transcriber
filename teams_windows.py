"""WebView2 document-start microphone guard and read-only meeting chat bridge.

No Python API is exposed to the Teams page. Only bounded chat snapshots from
trusted HTTPS Teams documents are accepted, including child frames.
"""
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from paths import RESOURCE_DIR


class WindowsTeamsChat:
    def __init__(self):
        self._frames = {}
        self._lock = threading.Lock()
        self._handlers = []

    def receive(self, source, body):
        try:
            url = urlsplit(str(source))
            if (url.scheme != 'https' or url.hostname not in
                    ('teams.microsoft.com', 'teams.cloud.microsoft') or
                    url.port not in (None, 443) or not isinstance(body, str) or len(body) > 1500000):
                return
            data = json.loads(body)
            if not isinstance(data, dict) or data.get('kind') != 'chat' or not isinstance(data.get('messages'), list):
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

    def install(self, browser):
        def message(sender, args):
            try:
                self.receive(str(args.Source), str(args.TryGetWebMessageAsString()))
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
                               ('teams-mic-guard.js', 'teams-chat.js'))
            return core.AddScriptToExecuteOnDocumentCreatedAsync(source)

        # Wait off the UI thread; first navigation is held on a local placeholder until
        # WebView2 confirms that the document-start scripts are registered.
        task = browser.with_core_webview2(attach)
        task.GetAwaiter().GetResult()
