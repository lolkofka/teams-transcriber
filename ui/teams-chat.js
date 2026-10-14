// Read only the currently rendered meeting chat; never send messages or open private chats.
(() => {
  const host = location.hostname;
  if (!['teams.microsoft.com', 'teams.cloud.microsoft'].includes(host) && !host.endsWith('.teams.microsoft.com')) return;
  const bridge = window.webkit?.messageHandlers?.teamsAutomation || window.chrome?.webview;
  if (!bridge || window.__teamsChat) return;
  const frame = crypto.randomUUID();
  const clean = e => (e?.innerText || e?.textContent || '').trim();
  const headingName = e => (e.getAttribute('aria-label') || clean(e)).trim();
  const authorFrom = h => headingName(h).match(/(?:,\s*автор:\s*|\s+by\s+)(.+)$/i)?.[1]?.trim();
  function snapshot() {
    const title = [...document.querySelectorAll('h2,[role="heading"][aria-level="2"]')]
      .find(e => /^(чат собрания|meeting chat)$/i.test(clean(e)));
    const messages = [];
    if (title) {
      // The panel contains the title and a message list, separate from the app's sidebar.
      let panel = title.parentElement;
      for (let i = 0; panel && i < 6; i++, panel = panel.parentElement) {
        if (panel === document.body || panel.querySelectorAll('h2,[role="heading"][aria-level="2"]').length > 1) { panel = null; break; }
        if (panel.querySelector('h4,[role="heading"][aria-level="4"]')) break;
      }
      if (panel && !panel.contains(document.querySelector('[role="navigation"]'))) {
        for (const h of panel.querySelectorAll('h4,[role="heading"][aria-level="4"]')) {
          const author = authorFrom(h); if (!author) continue;
          let root = h.parentElement, body, stamp;
          for (let i = 0; root && root !== panel && i < 6; i++, root = root.parentElement) {
            if (root.querySelectorAll('h4,[role="heading"][aria-level="4"]').length > 1) break;
            body = root.querySelector('[data-tid="message-body"],[data-testid="message-body"],[data-tid="chat-pane-message"]');
            stamp = root.querySelector('time,[data-tid="message-timestamp"],[data-tid="timestamp"]') ||
              [...root.querySelectorAll('[title],[aria-label]')].find(e => {
                const label = e.getAttribute('title') || e.getAttribute('aria-label') || '';
                return e !== h && label.length < 120 && /\b\d{1,2}:\d{2}\b/.test(label);
              });
            // Teams' accessible message body has its own label and contains the text blocks.
            if (!body) body = [...root.querySelectorAll('[aria-label]')].reverse().find(e =>
              e !== h && e.getAttribute('aria-label').length > 40 && clean(e) && !e.querySelector('h4'));
            if (body && stamp) break;
          }
          if (!body || !stamp || !root || root === panel) continue;
          const text = clean(body); if (!text) continue;
          const datetime = stamp.getAttribute('datetime') || '';
          const timeLabel = stamp.getAttribute('title') || stamp.getAttribute('aria-label') || clean(stamp);
          const messageRoot = h.closest('[data-message-id],[data-tid="chat-pane-item"],[id^="message-"]');
          const id = messageRoot?.getAttribute('data-message-id') || messageRoot?.id || '';
          messages.push({id, author, text: text.slice(0, 20000), datetime, timeLabel});
          if (messages.length >= 50) break;
        }
      }
    }
    bridge.postMessage(JSON.stringify({kind: 'chat', frame, messages}));
    return messages;
  }
  window.__teamsChat = {snapshot};
  setInterval(snapshot, 1000);
})();
