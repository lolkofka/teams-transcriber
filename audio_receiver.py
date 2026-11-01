"""Mac endpoint for short incoming PCM WAV packets. No audio-driver dependency."""
import hmac
import io
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class AudioReceiver:
    def __init__(self, on_pcm, path, host='0.0.0.0', port=8767):
        if not path.startswith('/') or path == '/' or any(c.isspace() for c in path):
            raise ValueError('Укажите путь приёма, например /audio/receive')
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass  # The receive path may contain a private access key.

            def do_POST(self):
                if not hmac.compare_digest(self.path.encode(), path.encode()):
                    self.send_error(404)
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if self.headers.get('Transfer-Encoding') or not 44 < size <= 64044:
                        raise ValueError('Invalid packet size')
                    if self.headers.get('Content-Type') != 'audio/wav':
                        raise ValueError('Expected audio/wav')
                    raw = self.rfile.read(size)
                    if len(raw) != size:
                        raise ValueError('Incomplete packet')
                    with wave.open(io.BytesIO(raw), 'rb') as wav:
                        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, 'NONE'):
                            raise ValueError('Expected mono 16 kHz PCM16')
                        count = wav.getnframes()
                        if not 0 < count <= 32000:
                            raise ValueError('Invalid audio length')
                        pcm = wav.readframes(count)
                        if len(pcm) != count * 2:
                            raise ValueError('Incomplete audio')
                except (ValueError, wave.Error, EOFError, OSError):
                    self.send_error(400)
                    return
                try:
                    with owner.lock:
                        on_pcm(pcm)
                except Exception:
                    self.send_error(503, 'Audio output unavailable')
                    return
                self.send_response(204)
                self.send_header('Content-Length', '0')
                self.end_headers()

        self.http = ThreadingHTTPServer((host, port), Handler)
        self.http.daemon_threads = True
        self.port = self.http.server_port
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        if self.thread.is_alive():
            self.http.shutdown()
            self.thread.join(timeout=3)
        self.http.server_close()
