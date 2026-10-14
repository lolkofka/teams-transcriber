"""Mac integration check: real WKWebView + local WebRTC + native PCM + WAV.

Run with PYTHONPATH=. .venv/bin/python tests/webview_audio_smoke.py.
No account, microphone, network connection or virtual audio driver is used.
"""
import queue
import threading
import time
import tempfile
import wave

import numpy as np
import webview

from teams_audio import TeamsBrowserRecorder
from teams_browser import TeamsBrowser
from unittest.mock import patch


HTML = """<!doctype html><button id="begin">Start local test</button><script>
window.testStatus = 'waiting';
document.querySelector('button').onclick = async () => {
  try {
    window.sender = new RTCPeerConnection(); window.receiver = new RTCPeerConnection();
    const pending = [];
    function candidate(e, peer) {
      if (!e.candidate) return;
      const value = e.candidate.toJSON();
      // Both peers are on this Mac: use loopback instead of mDNS candidates,
      // which can be unreachable under a VPN or local-network permissions.
      const fields = value.candidate.split(' '); fields[4] = '127.0.0.1'; value.candidate = fields.join(' ');
      if (peer.remoteDescription) peer.addIceCandidate(value).catch(e => window.testStatus=String(e));
      else pending.push([peer, value]);
    }
    sender.onicecandidate = e => candidate(e, receiver);
    receiver.onicecandidate = e => candidate(e, sender);
    window.ctx = new AudioContext(); await ctx.resume();
    window.tone = ctx.createOscillator(); tone.frequency.value = 440;
    const gain = window.testGain = ctx.createGain(); gain.gain.value = 0.2;
    const stream = window.testStream = ctx.createMediaStreamDestination();
    tone.connect(gain); gain.connect(stream); tone.start();
    receiver.ontrack = event => {
      window.audio = document.createElement('audio'); audio.srcObject = new MediaStream([event.track]);
      audio.autoplay = true; document.body.append(audio); audio.play().catch(e => window.playError=String(e));
    };
    sender.addTrack(stream.stream.getAudioTracks()[0], stream.stream);
    await sender.setLocalDescription(await sender.createOffer());
    await receiver.setRemoteDescription(sender.localDescription);
    await receiver.setLocalDescription(await receiver.createAnswer());
    await sender.setRemoteDescription(receiver.localDescription);
    for (const [peer, value] of pending) await peer.addIceCandidate(value);
    window.testStatus = 'negotiated';
  } catch (e) { window.testStatus = String(e); }
};
</script>"""

window = webview.create_window("Local Teams audio test", html="<p>Preparing local test</p>")
browser = TeamsBrowser()
with patch("teams_browser.webview.create_window", return_value=window):
    browser.open()
tap = browser.audio
result = {}


def check():
    recorder = None
    try:
        assert tap.installed.wait(10), tap.error
        assert window.events.loaded.wait(10), "Initial test document failed to load"
        from PyObjCTools import AppHelper
        from Foundation import NSURL
        from webview.platforms.cocoa import BrowserView
        # A synthetic document with the Teams origin exercises the production
        # origin checks and document-start injection without contacting Teams.
        window.events.loaded.clear()
        AppHelper.callAfter(lambda: BrowserView.instances[window.uid].webview.loadHTMLString_baseURL_(
            HTML, NSURL.URLWithString_("https://teams.microsoft.com/")))
        assert window.events.loaded.wait(15), "Local document failed to load"
        assert window.evaluate_js("Boolean(window.__teamsAudioTap)"), "Tap was not injected at document start"
        with tempfile.TemporaryDirectory() as folder:
            target = queue.Queue()
            recorder = TeamsBrowserRecorder(browser, target, folder + "/audio.wav")
            recorder.start()
            browser.set_volume(0)
            window.evaluate_js("document.querySelector('button').click()")
            deadline = time.monotonic() + 12
            chunks = []
            while time.monotonic() < deadline:
                try:
                    value = target.get(timeout=.5)
                    if value is not None:
                        chunks.append(value[1])
                        if len(chunks) >= 10 and np.max(np.abs(np.concatenate(chunks))) > .05:
                            break
                except queue.Empty:
                    pass
            recorder.stop_event.set(); recorder.join(3)
            assert recorder.error is None, recorder.error
            peak = float(np.max(np.abs(np.concatenate(chunks)))) if chunks else 0
            assert peak > .05, (peak, window.evaluate_js("({status:window.testStatus,tap:window.__teamsAudioTap.status(),connection:receiver.connectionState})"), tap.error)
            assert browser.evaluate_js("audio.volume") == 0, "Playback was not muted"
            browser.set_volume(40)
            deadline = time.monotonic() + 3
            while browser.evaluate_js("audio.volume") != 0.4 and time.monotonic() < deadline:
                time.sleep(.1)
            assert browser.evaluate_js("audio.volume") == 0.4, "Playback volume was not restored"
            with wave.open(folder + "/audio.wav") as wav:
                assert wav.getframerate() == 16000 and wav.getnframes() > 0
            # Stopping our recording must leave the underlying call alive.
            assert window.evaluate_js("receiver.connectionState") == "connected"
            recorder = TeamsBrowserRecorder(browser, queue.Queue())
            recorder.start()
            time.sleep(.7)
            recorder.stop_event.set(); recorder.join(3)
            restarted = []
            while not recorder.q.empty():
                item = recorder.q.get_nowait()
                if item is not None:
                    restarted.append(item[1])
            assert restarted and np.max(np.abs(np.concatenate(restarted))) > .05, "Restart lost the call audio"
            window.evaluate_js("receiver.close()")
            assert window.evaluate_js("window.__teamsAudioTap.status().tracks") == 0
            result.update(ok=True, peak=peak, chunks=len(chunks))
    except Exception as exc:
        result["error"] = repr(exc)
    finally:
        if recorder:
            recorder.stop_event.set()
        window.destroy()


timer = threading.Timer(35, window.destroy); timer.daemon = True; timer.start()
webview.start(check, private_mode=False)
timer.cancel()
print(result)
if not result.get("ok"):
    raise RuntimeError(result.get("error", "WebView test timed out"))
