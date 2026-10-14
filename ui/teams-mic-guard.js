// Incoming audio is untouched. Outgoing microphone audio needs an explicit user action.
(() => {
  const host = location.hostname;
  if (!(host === 'teams.microsoft.com' || host.endsWith('.teams.microsoft.com') || host === 'teams.cloud.microsoft') || window.__teamsMicGuard) return;
  let allowed = false, clicking = false, lastClick = 0, wasPrejoin = false;
  const tracks = new Set(), protectedTracks = new WeakSet();
  const state = window.__teamsMicGuard = {status: () => ({allowed, tracks: tracks.size})};
  function deny() {
    allowed = false;
    for (const track of tracks) track.enabled = false;
  }
  state.reset = deny;
  function protect(track) {
    if (track.kind !== 'audio' || protectedTracks.has(track)) return track;
    let prototype = track, descriptor;
    while (prototype && !descriptor) {
      descriptor = Object.getOwnPropertyDescriptor(prototype, 'enabled');
      prototype = Object.getPrototypeOf(prototype);
    }
    if (!descriptor?.get || !descriptor?.set) {
      track.stop(); throw new Error('Cannot protect outgoing microphone track');
    }
    if (!allowed) descriptor.set.call(track, false);
    Object.defineProperty(track, 'enabled', {
      configurable: false,
      get: () => descriptor.get.call(track),
      set: value => descriptor.set.call(track, !!value && allowed),
    });
    protectedTracks.add(track); tracks.add(track);
    track.addEventListener('ended', () => tracks.delete(track), {once: true});
    const clone = track.clone.bind(track);
    track.clone = () => protect(clone());
    return track;
  }
  const media = navigator.mediaDevices;
  if (media?.getUserMedia) {
    const original = media.getUserMedia.bind(media);
    media.getUserMedia = async constraints => {
      const stream = await original(constraints);
      for (const track of stream.getAudioTracks()) protect(track);
      return stream;
    };
  }
  const Original = window.RTCPeerConnection;
  if (Original) {
    window.RTCPeerConnection = new Proxy(Original, {
      construct(Target, args, NewTarget) {
        const pc = Reflect.construct(Target, args, NewTarget);
        const check = () => {
          if (['disconnected', 'failed', 'closed'].includes(pc.connectionState) ||
              ['disconnected', 'failed', 'closed'].includes(pc.iceConnectionState)) { deny(); enforce(); }
        };
        pc.addEventListener('connectionstatechange', check);
        pc.addEventListener('iceconnectionstatechange', check);
        return pc;
      }
    });
    if (window.webkitRTCPeerConnection === Original) window.webkitRTCPeerConnection = window.RTCPeerConnection;
  }
  const visible = e => e.getClientRects().length && !e.closest('[aria-hidden="true"]');
  function microphone() {
    const buttons = [...document.querySelectorAll('button,[role="button"],[role="switch"],input[type="checkbox"]')].filter(visible);
    const ids = ['microphone-button', 'mic-button', 'toggle-mute'];
    const identified = buttons.filter(e => ids.includes(e.getAttribute('data-tid') || e.getAttribute('data-testid') || e.id));
    const choices = identified.length ? identified : buttons.filter(e => /^(?:unmute\b|mute\b|(?:включить|выключить|отключить)\s+микрофон(?:\s|$))/i.test(e.getAttribute('aria-label') || e.innerText || ''));
    if (choices.length !== 1) return null;
    const button = choices[0], name = button.getAttribute('aria-label') || button.innerText || '';
    let on = null;
    if (/^(?:unmute\b|включить(?:\s|$))/i.test(name)) on = false;
    else if (/^(?:mute\b|(?:выключить|отключить)(?:\s|$))/i.test(name)) on = true;
    else {
      const checked = button.getAttribute('aria-checked') ?? button.getAttribute('aria-pressed');
      if (checked === 'true' || checked === 'false') on = checked === 'true';
      else if (button.matches('input[type="checkbox"]')) on = button.checked;
    }
    return {button, on};
  }
  function explicitAction(event) {
    if (!event.isTrusted || clicking) return;
    const mic = microphone();
    if (!mic || mic.on === null) return;
    const isClick = event.type === 'click' && mic.button.contains(event.target);
    const isShortcut = event.type === 'keydown' && event.key?.toLowerCase() === 'm' && event.shiftKey && (event.metaKey || event.ctrlKey);
    if (!isClick && !isShortcut) return;
    allowed = !mic.on;
    if (!allowed) deny();
  }
  document.addEventListener('click', explicitAction, true);
  document.addEventListener('keydown', explicitAction, true);
  function enforce() {
    const prejoin = [...document.querySelectorAll('button')].some(e => visible(e) && /^(join now|присоединиться сейчас)$/i.test((e.getAttribute('aria-label') || e.innerText || '').trim()));
    if (prejoin && !wasPrejoin) deny();
    wasPrejoin = prejoin;
    const mic = microphone();
    if (!allowed && mic?.on === true && !mic.button.disabled && Date.now() - lastClick > 1000) {
      clicking = true; lastClick = Date.now();
      try { mic.button.click(); } finally { clicking = false; }
    }
  }
  window.addEventListener('offline', () => { deny(); enforce(); });
  window.addEventListener('online', () => { deny(); enforce(); });
  const observe = () => {
    new MutationObserver(enforce).observe(document.documentElement, {subtree: true, childList: true, attributes: true, attributeFilter: ['aria-label', 'aria-checked', 'aria-pressed', 'disabled']});
    enforce();
  };
  if (document.documentElement) observe();
  else document.addEventListener('DOMContentLoaded', observe, {once: true});
  setInterval(enforce, 250);
})();
