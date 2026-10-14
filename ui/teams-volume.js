// Playback gain only: captured PCM and microphone tracks are untouched.
(() => {
  const host = location.hostname;
  if (!(host === 'teams.microsoft.com' || host.endsWith('.teams.microsoft.com') || host === 'teams.cloud.microsoft') || window.__teamsPlaybackVolume) return;
  let level = Math.max(0, Math.min(1, Number(window.__transcriberInitialVolume ?? 1)));
  const masters = new Map(), media = new Map();
  const routed = new WeakSet();
  const connect = AudioNode.prototype.connect, disconnect = AudioNode.prototype.disconnect;
  function master(context) {
    if (!masters.has(context)) {
      const gain = context.createGain(); gain.gain.value = level;
      connect.call(gain, context.destination);
      masters.set(context, gain);
    }
    return masters.get(context);
  }
  AudioNode.prototype.connect = function (destination, ...args) {
    if (!this.context.__transcriberCapture && destination === this.context.destination) {
      connect.call(this, master(this.context), args[0] ?? 0, 0);
      return destination;
    }
    return connect.call(this, destination, ...args);
  };
  AudioNode.prototype.disconnect = function (...args) {
    if (!this.context.__transcriberCapture && args[0] === this.context.destination && masters.has(this.context)) {
      args[0] = masters.get(this.context);
    }
    return disconnect.apply(this, args);
  };
  const volume = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'volume');
  function remember(element) {
    if (!media.has(element)) media.set(element, volume.get.call(element));
    volume.set.call(element, media.get(element) * (routed.has(element) ? 1 : level));
  }
  Object.defineProperty(HTMLMediaElement.prototype, 'volume', {
    configurable: volume.configurable, enumerable: volume.enumerable,
    get: volume.get,
    set(value) {
      // Let WebKit validate the range/type, then keep Teams' own intended volume.
      volume.set.call(this, value);
      media.set(this, volume.get.call(this));
      volume.set.call(this, media.get(this) * (routed.has(this) ? 1 : level));
    }
  });
  function scan() {
    for (const element of document.querySelectorAll('audio,video')) remember(element);
    for (const element of media.keys()) remember(element);
    for (const context of masters.keys()) if (context.state === 'closed') masters.delete(context);
  }
  const createMediaElementSource = AudioContext.prototype.createMediaElementSource;
  AudioContext.prototype.createMediaElementSource = function (element) {
    const node = createMediaElementSource.call(this, element);
    routed.add(element); remember(element);
    return node;
  };
  window.__teamsPlaybackVolume = {
    setVolume(value) {
      value = Number(value);
      if (!Number.isFinite(value)) return false;
      level = Math.max(0, Math.min(1, value));
      for (const [context, gain] of masters) if (context.state !== 'closed') gain.gain.value = level;
      scan();
      return true;
    },
    status: () => ({level, contexts: masters.size, elements: media.size, gains: [...masters.values()].map(g => g.gain.value)})
  };
  const observe = () => { new MutationObserver(scan).observe(document.documentElement, {subtree:true, childList:true}); scan(); };
  if (document.documentElement) observe();
  else document.addEventListener('DOMContentLoaded', observe, {once:true});
})();
