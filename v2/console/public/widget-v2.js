/* Embed: <script src="https://YOUR-CONSOLE/widget-v2.js" data-channel="CONNECTION_ID" async></script> */
(() => {
  const script = document.currentScript;
  if (!script || !script.dataset.channel) return;
  const origin = new URL(script.src).origin;
  const channel = script.dataset.channel;
  const host = document.createElement('div');
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `<style>
    :host{font:14px system-ui,sans-serif;position:fixed;bottom:20px;right:20px;z-index:2147483000;color:#233b34}*{box-sizing:border-box}button{cursor:pointer;font:inherit;border:0;border-radius:8px;background:#17664e;color:white;padding:11px 16px}button:focus-visible,input:focus-visible{outline:3px solid #aacd90;outline-offset:3px}.bubble{border-radius:30px;box-shadow:0 5px 25px #143b2433}.panel{width:min(360px,calc(100vw - 32px));height:min(530px,calc(100dvh - 100px));background:white;border:1px solid #dce5d8;border-radius:15px;box-shadow:0 12px 55px #173d2533;overflow:hidden;display:flex;flex-direction:column}header{padding:17px;background:#17372d;color:#eaf4e4;display:flex;justify-content:space-between;align-items:center}header button{background:none;padding:0 6px;font-size:23px}header small{display:block;color:#b5c6af;font-size:11px;margin-top:4px}.messages{flex:1;overflow:auto;padding:16px;background:#f6f8f2}.message{white-space:pre-wrap;overflow-wrap:anywhere;padding:10px 13px;border-radius:10px;margin:9px 0;max-width:90%;line-height:1.6;background:white;border:1px solid #e1e9dc}.outbound{background:#e9f0df}.inbound{margin-left:auto}form{display:flex;gap:8px;padding:12px;border-top:1px solid #e0e8da}input{font:inherit;min-width:0;flex:1;border:1px solid #dce5d8;border-radius:8px;padding:10px}.error{font-size:11px;color:#943c2d;padding:0 12px 10px}[hidden]{display:none!important}
    </style><button class="bubble" aria-expanded="false">Chat with our team</button><section class="panel" aria-label="Customer chat" hidden><header><div><strong>Customer support</strong><small>Your conversation with our team</small></div><button class="close" aria-label="Close chat">×</button></header><div class="messages" aria-live="polite"></div><form><input aria-label="Your message" required maxlength="4000" placeholder="Write a message…"/><button type="submit">Send</button></form><div class="error" role="alert"></div></section>`;
  document.body.appendChild(host);
  const bubble = root.querySelector('.bubble'), panel = root.querySelector('.panel'), messages = root.querySelector('.messages');
  const form = root.querySelector('form'), input = root.querySelector('input'), send = form.querySelector('button'), error = root.querySelector('.error');
  const storageKey = 'itura-visitor:' + origin + ':' + channel;
  let session = null, timer = null, request = null, inFlight = false;
  try { session = sessionStorage.getItem(storageKey); } catch { /* Private browsing still works in memory. */ }
  async function call(path, method = 'GET', body) {
    const response = await fetch(origin + path, { method, credentials: 'omit', headers: { 'Content-Type': 'application/json', ...(session ? { Authorization: 'Bearer ' + session } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) { session = null; try { sessionStorage.removeItem(storageKey); } catch {} }
      throw new Error(typeof data.detail === 'string' ? data.detail : 'Please try again.');
    }
    return data;
  }
  async function ensureSession() {
    if (session) return;
    const data = await call('/web/' + encodeURIComponent(channel) + '/sessions', 'POST', { name: '' });
    session = data.token;
    try { sessionStorage.setItem(storageKey, session); } catch {}
  }
  async function refresh() {
    if (inFlight || panel.hidden || !session) return;
    inFlight = true;
    try {
      const data = await call('/web/messages');
      const last = messages.lastElementChild?.dataset.id;
      if (last !== data.at(-1)?.id) {
        messages.replaceChildren(...data.map(item => {
          const element = document.createElement('div'); element.className = 'message ' + item.direction; element.dataset.id = item.id;
          element.textContent = item.body; return element;
        }));
        messages.scrollTop = messages.scrollHeight;
      }
      error.textContent = '';
    } catch (e) { error.textContent = e.message; }
    finally { inFlight = false; }
  }
  bubble.addEventListener('click', async () => {
    panel.hidden = false; bubble.hidden = true; bubble.setAttribute('aria-expanded', 'true'); input.focus();
    try { await ensureSession(); await refresh(); timer = setInterval(refresh, 4000); }
    catch (e) { error.textContent = e.message; }
  });
  root.querySelector('.close').addEventListener('click', () => {
    panel.hidden = true; bubble.hidden = false; bubble.setAttribute('aria-expanded', 'false'); clearInterval(timer); bubble.focus();
  });
  input.addEventListener('input', () => { request = null; });
  form.addEventListener('submit', async e => {
    e.preventDefault(); if (!input.value.trim() || send.disabled) return;
    send.disabled = true; request = request || crypto.randomUUID();
    try { await ensureSession(); await call('/web/messages', 'POST', { body: input.value.trim(), request_id: request }); input.value = ''; request = null; await refresh(); }
    catch (e) { error.textContent = e.message; }
    finally { send.disabled = false; input.focus(); }
  });
})();
