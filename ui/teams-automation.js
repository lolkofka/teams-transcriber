// Calendar and call controls from each trusted frame of our own WKWebView.
(() => {
  const host = location.hostname || (() => { try { return parent.location.hostname; } catch (_) { return ''; } })();
  const trusted = host === 'teams.microsoft.com' || host.endsWith('.teams.microsoft.com') ||
    host === 'teams.cloud.microsoft' || ['outlook.office.com', 'outlook.office365.com', 'outlook.cloud.microsoft'].includes(host);
  if (!trusted || window.__teamsAutomation) return;
  const bridge = window.webkit?.messageHandlers?.teamsAutomation;
  if (!bridge) return;
  const frame = crypto.randomUUID(), ids = new WeakMap();
  let next = 0, nodes = new Map();
  function name(e) {
    const labelled = e.getAttribute('aria-labelledby');
    return (e.getAttribute('aria-label') || (labelled && labelled.split(/\s+/).map(id => document.getElementById(id)?.textContent || '').join(' ')) ||
      (e.labels && Array.from(e.labels).map(l => l.textContent).join(' ')) || e.getAttribute('title') || e.innerText || '').trim();
  }
  function visible(e) {
    const style = getComputedStyle(e);
    return e.isConnected && e.getClientRects().length > 0 && style.visibility !== 'hidden' && style.display !== 'none' && !e.closest('[aria-hidden="true"]');
  }
  function snapshot() {
    const controls = [], current = new Map();
    for (const e of document.querySelectorAll('button,[role="button"],[role="tab"],[role="switch"],[role="checkbox"],input[type="checkbox"],a[href],[aria-label*="Microsoft Teams Meeting"]')) {
      if (controls.length >= 1000) break;
      if (!visible(e)) continue;
      const label = name(e); if (!label) continue;
      if (!ids.has(e)) ids.set(e, String(++next));
      const id = ids.get(e); current.set(id, e);
      let checked = null;
      if (e.matches('input[type="checkbox"]')) checked = e.checked;
      else for (const attribute of ['aria-checked', 'aria-pressed']) {
        const value = e.getAttribute(attribute);
        if (value === 'true' || value === 'false') { checked = value === 'true'; break; }
      }
      controls.push({id, name: label.slice(0, 2000), automationId: e.getAttribute('data-tid') || e.getAttribute('data-testid') || e.id || '',
        checked, disabled: !!e.disabled || e.getAttribute('aria-disabled') === 'true'});
    }
    nodes = current;
    bridge.postMessage(JSON.stringify({frame, controls}));
  }
  window.__teamsAutomation = {
    snapshot,
    invoke(id, expectedName) {
      const e = nodes.get(id);
      if (!e || !visible(e) || e.disabled || e.getAttribute('aria-disabled') === 'true' || name(e) !== expectedName) return false;
      e.click(); snapshot(); return true;
    }
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', snapshot, {once:true});
  else snapshot();
  setInterval(snapshot, 500);
})();
