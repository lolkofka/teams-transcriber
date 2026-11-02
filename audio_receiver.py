"""Mac endpoint for short incoming PCM WAV packets. No audio-driver dependency."""
import hmac
import io
import array
import json
import math
import socket
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class AudioReceiver:
    def __init__(self, on_pcm, path, host='0.0.0.0', port=8767,
                 status_provider=None, on_error=None):
        if not path.startswith('/') or path == '/' or any(c.isspace() for c in path):
            raise ValueError('Укажите путь приёма, например /audio/receive')
        self.lock = threading.Lock()
        self.received_packets = 0
        self.received_bytes = 0
        self.peak_db = -100.0
        self.last_packet = None
        self.last_error = ''
        self.status_provider = status_provider
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def setup(self):
                super().setup()
                self.connection.settimeout(5)
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            def log_message(self, *args):
                pass  # The receive path may contain a private access key.

            def do_GET(self):
                if not hmac.compare_digest(self.path.encode(), (path + '/status').encode()):
                    self.send_error(404)
                    return
                data = json.dumps(owner.status(), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)

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
                        owner.received_packets += 1
                        owner.received_bytes += len(pcm)
                        owner.last_packet = time.monotonic()
                        samples = array.array('h', pcm)
                        if sys.byteorder != 'little':
                            samples.byteswap()
                        peak = max(abs(min(samples)), abs(max(samples))) / 32768
                        owner.peak_db = round(max(-100, 20 * math.log10(max(peak, 1e-5))), 2)
                        owner.last_error = ''
                except Exception as exc:
                    message = str(exc)
                    with owner.lock:
                        changed = message != owner.last_error
                        owner.last_error = message
                    if on_error is not None and changed:
                        on_error(message)
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

    def status(self):
        with self.lock:
            result = {'received_packets': self.received_packets, 'received_bytes': self.received_bytes,
                      'peak_db': self.peak_db, 'error': self.last_error,
                      'packet_age_seconds': (None if self.last_packet is None else
                                             round(time.monotonic() - self.last_packet, 2))}
        if self.status_provider is not None:
            result['playback'] = self.status_provider()
        return result

    def close(self):
        if self.thread.is_alive():
            self.http.shutdown()
            self.thread.join(timeout=3)
        self.http.server_close()
