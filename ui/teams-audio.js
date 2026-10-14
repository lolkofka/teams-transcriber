// Injected at document start in our Teams WKWebView, including Teams frames.
// Only received WebRTC tracks enter this graph; getUserMedia is untouched.
(() => {
  const allowed = h => h === 'teams.microsoft.com' || h.endsWith('.teams.microsoft.com') || h === 'teams.cloud.microsoft';
  if (!allowed(location.hostname) || window.__teamsAudioTap) return;
  const bridge = window.webkit?.messageHandlers?.teamsAudio;
  if (!bridge || !window.RTCPeerConnection) return;
  const state = window.__teamsAudioTap = { tracks: new Map(), error: null };
  const frame = crypto.randomUUID();
  let context, processor, mute;
  state.status = () => ({tracks: state.tracks.size, context: context?.state, error: state.error});
  function report(error) {
    state.error = String(error);
    bridge.postMessage(JSON.stringify({kind: 'error', error: state.error}));
  }
  function resume() { if (context?.state === 'suspended') context.resume().catch(report); }
  document.addEventListener('click', resume, true);
  document.addEventListener('keydown', resume, true);
  function attach(track) {
    if (track.kind !== 'audio' || track.readyState === 'ended' || state.tracks.has(track.id)) return;
    try {
      if (!context) {
        context = new AudioContext();
        context.__transcriberCapture = true;
        // ScriptProcessor avoids fetching a worklet module through Teams' CSP.
        processor = context.createScriptProcessor(4096, 1, 1);
        mute = context.createGain(); mute.gain.value = 0;
        processor.connect(mute); mute.connect(context.destination);
        processor.onaudioprocess = event => {
          if (!state.tracks.size) return;
          const input = event.inputBuffer.getChannelData(0);
          const bytes = new Uint8Array(input.length * 2);
          const view = new DataView(bytes.buffer);
          for (let i = 0; i < input.length; i++) view.setInt16(i * 2, Math.round(Math.max(-1, Math.min(1, input[i])) * 32767), true);
          let binary = '';
          for (const byte of bytes) binary += String.fromCharCode(byte);
          bridge.postMessage(JSON.stringify({kind: 'pcm', frame, rate: context.sampleRate,
            time: (performance.timeOrigin + performance.now()) / 1000 - input.length / context.sampleRate,
            pcm: btoa(binary), tracks: state.tracks.size}));
        };
      }
      const source = context.createMediaStreamSource(new MediaStream([track]));
      source.connect(processor);
      state.tracks.set(track.id, source);
      const remove = () => { source.disconnect(); state.tracks.delete(track.id); };
      track.addEventListener('ended', remove, {once: true});
      resume();
    } catch (error) { report(error); }
  }
  const Original = window.RTCPeerConnection;
  window.RTCPeerConnection = new Proxy(Original, {
    construct(Target, args, NewTarget) {
      const pc = Reflect.construct(Target, args, NewTarget);
      pc.addEventListener('track', event => attach(event.track));
      const cleanup = () => {
        for (const receiver of pc.getReceivers()) {
          const source = state.tracks.get(receiver.track?.id);
          if (source) { source.disconnect(); state.tracks.delete(receiver.track.id); }
        }
      };
      const close = pc.close;
      pc.close = function (...args) { cleanup(); return Reflect.apply(close, this, args); };
      pc.addEventListener('connectionstatechange', () => {
        if (pc.connectionState === 'closed') cleanup();
      });
      return pc;
    }
  });
  if (window.webkitRTCPeerConnection === Original) window.webkitRTCPeerConnection = window.RTCPeerConnection;
  bridge.postMessage(JSON.stringify({kind: 'ready'}));
})();
