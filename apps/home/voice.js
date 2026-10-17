/*
 * Parsec, the assistant: on every page, by voice or typing. "Hey Parsec" works when "Listen for Hey Parsec" is on.
 * It listens with the browser's own speech recognition (Chrome and Edge), shows what it heard, carries out screen
 * commands itself (full screen, zoom, scroll, views, home, print) through hooks the page gives it, and passes
 * everything else to the page's handler. It reads answers aloud. Nothing here calls a model.
 */
(function (root) {
  'use strict';

  const Recognition = root.SpeechRecognition || root.webkitSpeechRecognition;
  const ALOUD_KEY = 'fineprint-read-aloud';
  const WAKE_KEY = 'fineprint-listen-wake';
  const CLOUD_OK_KEY = 'fineprint-wake-cloud-ok';   // consent to always-on listening through the online service
  const LANG = navigator.language || 'en-US';
  const AWAIT_MS = 8000;   // after a bare "Hey Parsec", how long the next phrase counts as the command
  const SCREEN = new Set(['fullscreen', 'exit-fullscreen', 'zoom-in', 'zoom-out', 'scroll-down', 'scroll-up', 'view',
    'go-home', 'print', 'read', 'close']);

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === 'text') node.textContent = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    });
    children.filter(Boolean).forEach((c) => node.append(c));
    return node;
  }

  function stored(key, fallback) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : v === '1'; } catch (err) { return fallback; }
  }
  function store(key, on) {
    try { localStorage.setItem(key, on ? '1' : '0'); } catch (err) { /* private mode: not remembered */ }
  }

  function mount(options) {
    const opts = options || {};
    const AS = root.FinePrintAssistant;
    const hooks = opts.screen || {};
    const log = el('ol', { class: 'voice-log', 'aria-live': 'polite' });
    const input = el('input', { type: 'text', class: 'voice-input', autocomplete: 'off', 'aria-label': 'Ask Parsec',
      placeholder: 'Ask, e.g. "what\'s wrong with the contract"' });
    const mic = el('button', { type: 'button', class: 'voice-mic', 'aria-pressed': 'false', title: 'Speak once' },
      el('span', { class: 'voice-mic-dot', 'aria-hidden': 'true' }), el('span', { text: 'Speak' }));
    const aloud = el('input', { type: 'checkbox', id: 'voice-aloud' });
    aloud.checked = stored(ALOUD_KEY, true);
    const wakeBox = el('input', { type: 'checkbox', id: 'voice-wake' });
    const privacy = el('p', { class: 'voice-privacy', 'aria-live': 'polite' });
    const form = el('form', { class: 'voice-form' }, input, el('button', { type: 'submit', class: 'voice-send', text: 'Send' }));
    const panel = el('section', { class: 'voice-panel', id: 'voice-panel', 'aria-label': 'Parsec, the assistant', hidden: '' },
      el('header', { class: 'voice-head' },
        el('strong', { text: 'Parsec' }),
        el('button', { type: 'button', class: 'voice-close', 'aria-label': 'Close Parsec', text: 'Close', onclick: () => toggle(false) })),
      el('div', { class: 'voice-options' },
        el('label', { class: 'voice-opt', for: 'voice-wake', title: 'Parsec listens for its name while this is on. Where the browser can recognise speech on this computer, no audio leaves it.' },
          wakeBox, el('span', { text: 'Listen for "Hey Parsec"' })),
        el('label', { class: 'voice-opt', for: 'voice-aloud' }, aloud, el('span', { text: 'Read answers aloud' })),
        privacy),
      log,
      el('div', { class: 'voice-controls' }, mic, form));
    const fab = el('button', { type: 'button', class: 'voice-fab', 'aria-expanded': 'false', 'aria-controls': 'voice-panel', title: 'Parsec: ask by voice or by typing' },
      el('span', { class: 'voice-fab-icon', 'aria-hidden': 'true', text: '\u{1F3A4}' }), el('span', { text: 'Ask Parsec' }),
      el('span', { class: 'voice-fab-live', 'aria-hidden': 'true' }));
    document.body.append(panel, fab);

    let lastReply = '';
    let speaking = false;

    function toggle(open) {
      panel.hidden = !open;
      fab.setAttribute('aria-expanded', String(open));
      if (open) input.focus();
    }
    fab.addEventListener('click', () => toggle(panel.hidden));

    function post(who, text) {
      const item = el('li', { class: `voice-msg voice-${who}` }, el('span', { class: 'voice-who', text: who === 'you' ? 'You' : 'Parsec' }), el('p', { text }));
      log.append(item);
      item.scrollIntoView({ block: 'nearest' });
    }

    function quiet() {
      if ('speechSynthesis' in root) root.speechSynthesis.cancel();
      speaking = false;
    }
    function speak(text) {
      if (!aloud.checked || !('speechSynthesis' in root) || !text) return;
      quiet();
      const u = new SpeechSynthesisUtterance(text);
      u.onstart = () => { speaking = true; };
      u.onend = () => { speaking = false; };
      u.onerror = () => { speaking = false; };
      root.speechSynthesis.speak(u);
    }
    aloud.addEventListener('change', () => { store(ALOUD_KEY, aloud.checked); if (!aloud.checked) quiet(); });

    // Screen commands are the same everywhere; the page says what full screen, zoom and the views mean for it.
    function screen(c) {
      switch (c.action) {
        case 'fullscreen':
          if (hooks.fullscreen) return hooks.fullscreen(true);
          document.documentElement.requestFullscreen().catch(() => {});
          return 'Full screen. Press F11 if your browser asks for a key press first.';
        case 'exit-fullscreen':
          if (hooks.fullscreen) return hooks.fullscreen(false);
          if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
          return 'Back to the normal view.';
        case 'zoom-in': return hooks.zoom ? hooks.zoom(1) : 'Zoom works on an open document.';
        case 'zoom-out': return hooks.zoom ? hooks.zoom(-1) : 'Zoom works on an open document.';
        case 'scroll-down':
        case 'scroll-up': {
          const target = (hooks.scrollTarget && hooks.scrollTarget()) || root;
          const step = (target === root ? root.innerHeight : target.clientHeight) * 0.8;
          target.scrollBy({ top: c.action === 'scroll-down' ? step : -step, behavior: 'smooth' });
          return '';
        }
        case 'view': return hooks.view ? hooks.view(c.view) : 'Open a deal first.';
        case 'go-home': root.location.href = '/'; return 'Going to your deals.';
        case 'print': setTimeout(() => root.print(), 300); return 'Opening the print dialog.';
        case 'close': toggle(false); return '';
        case 'read': return lastReply || 'There is nothing to repeat yet.';
        default: return '';
      }
    }

    async function ask(text, opened) {
      const said = String(text || '').trim();
      if (!said) return;
      if (opened !== false) toggle(true);
      post('you', said);
      let reply;
      try {
        const c = AS ? AS.parseCommand(said, {}) : { action: 'unknown' };
        reply = SCREEN.has(c.action) ? await screen(c) : await opts.handle(said);
      } catch (err) {
        reply = `Something went wrong: ${err.message || err}`;
      }
      if (!reply) { quiet(); return; }
      post('assistant', reply);
      lastReply = reply;
      speak(reply);
    }

    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const text = input.value;
      input.value = '';
      ask(text);
    });

    /* ---- where speech is recognised: on this computer when the browser can, otherwise online ---- */
    let onDevice = false;
    let deviceState = 'unsupported';   // available | downloadable | downloading | unavailable | unsupported

    function configure(rec) {
      rec.lang = LANG;
      if (onDevice) rec.processLocally = true;   // the audio stays on this computer
    }

    function showPrivacy() {
      privacy.textContent = onDevice
        ? 'Speech is recognised on this computer: no audio leaves it.'
        : 'This browser recognises speech through its online service (Google or Microsoft): audio is sent there while Parsec listens. Your documents are never sent.';
      privacy.classList.toggle('is-local', onDevice);
    }

    async function checkDevice() {
      if (!Recognition || typeof Recognition.available !== 'function') { deviceState = 'unsupported'; return; }
      try { deviceState = await Recognition.available({ langs: [LANG], processLocally: true }); } catch (err) { deviceState = 'unsupported'; }
      onDevice = deviceState === 'available';
    }

    // Called from the switch's click, which the browser needs before it downloads the language pack.
    async function installDevice() {
      if (deviceState !== 'downloadable' || typeof Recognition.install !== 'function') return false;
      post('assistant', 'Downloading the speech pack so I can listen on this computer…');
      try {
        if (await Recognition.install({ langs: [LANG], processLocally: true })) { deviceState = 'available'; onDevice = true; }
      } catch (err) { /* stays online-only */ }
      showPrivacy();
      return onDevice;
    }

    /* ---- speaking once (the Speak button) ---- */
    let once = null;
    function speakOnce() {
      if (once) { once.stop(); return; }
      stopWake();
      quiet();
      const rec = new Recognition();
      configure(rec);
      rec.interimResults = true;
      rec.onresult = (e) => {
        const result = e.results[e.results.length - 1];
        input.value = result[0].transcript;
        if (result.isFinal) { input.value = ''; ask(result[0].transcript); }
      };
      rec.onerror = (e) => post('assistant', explain(e.error));
      rec.onend = () => { once = null; mic.classList.remove('is-listening'); mic.setAttribute('aria-pressed', 'false'); if (wakeBox.checked) startWake(); };
      once = rec;
      mic.classList.add('is-listening');
      mic.setAttribute('aria-pressed', 'true');
      rec.start();
    }

    function explain(error) {
      return {
        'not-allowed': 'Microphone access is blocked. Allow it from the address bar, or type instead.',
        'no-speech': 'I did not hear anything. Press Speak and try again.',
        network: 'Speech recognition needs an internet connection. You can type instead.',
      }[error] || `Listening stopped (${error}).`;
    }

    /* ---- always listening for "Hey Parsec" ---- */
    let wakeRec = null;
    let restarts = 0;   // quick consecutive endings, for the back-off
    let awaiting = 0;   // until when the next phrase is the command after a bare "Hey Parsec"

    function heard(transcript) {
      if (speaking) return;   // do not answer our own voice
      if (awaiting && Date.now() < awaiting) {
        awaiting = 0;
        ask(transcript);
        return;
      }
      const w = AS ? AS.wake(transcript) : { woke: false };
      if (!w.woke) return;
      if (w.rest) { ask(w.rest); return; }
      awaiting = Date.now() + AWAIT_MS;
      toggle(true);
      post('assistant', 'Yes?');
      speak('Yes?');
    }

    function startWake() {
      if (!Recognition || wakeRec || once || !wakeBox.checked) return;
      const rec = new Recognition();
      configure(rec);
      rec.continuous = true;
      rec.interimResults = false;
      rec.onresult = (e) => {
        for (let i = e.resultIndex; i < e.results.length; i += 1) {
          if (e.results[i].isFinal) heard(e.results[i][0].transcript);
        }
      };
      rec.onerror = (e) => {
        if (['network', 'language-not-supported', 'audio-capture'].includes(e.error)) {
          // Starting again would fail the same way: stop and say why, instead of looping.
          wakeBox.checked = false;
          fab.classList.remove('is-awake');
          post('assistant', `Stopped listening for "Hey Parsec": ${e.error === 'network' ? 'speech recognition is offline' : e.error === 'audio-capture' ? 'no microphone was found' : 'this language is not supported'}. Switch it on again to retry.`);
          return;
        }
        if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
          wakeBox.checked = false;
          store(WAKE_KEY, false);
          fab.classList.remove('is-awake');
          post('assistant', explain('not-allowed'));
        }
      };
      // The browser ends a session after a while or on silence: start again, waiting longer after quick failures.
      const began = Date.now();
      rec.onend = () => {
        wakeRec = null;
        restarts = Date.now() - began < 2000 ? restarts + 1 : 0;
        if (wakeBox.checked && !once) setTimeout(startWake, Math.min(30000, 400 * 2 ** restarts));
      };
      wakeRec = rec;
      try { rec.start(); } catch (err) { wakeRec = null; }
    }

    function stopWake() {
      if (wakeRec) { const rec = wakeRec; wakeRec = null; rec.onend = null; rec.abort(); }
    }

    async function setWake(on) {
      if (on && !onDevice && !(await installDevice())) {
        const ok = stored(CLOUD_OK_KEY, false) || root.confirm('This browser cannot recognise speech on this computer. '
          + 'Listening for "Hey Parsec" would send microphone audio to its online speech service (Google or Microsoft) '
          + 'the whole time it is switched on. Your documents are never sent. Turn it on anyway?');
        if (!ok) { wakeBox.checked = false; return; }
        store(CLOUD_OK_KEY, true);
      }
      wakeBox.checked = on;
      store(WAKE_KEY, on);
      fab.classList.toggle('is-awake', on);
      if (on) { startWake(); post('assistant', 'Listening for "Hey Parsec". Say it, then what you want, for example "Hey Parsec, show the findings".'); }
      else stopWake();
    }

    if (!Recognition) {
      mic.disabled = true;
      mic.title = 'Speaking needs Chrome or Edge. You can type instead.';
      wakeBox.disabled = true;
      wakeBox.parentElement.title = '"Hey Parsec" needs Chrome or Edge.';
    } else {
      mic.addEventListener('click', speakOnce);
      wakeBox.addEventListener('change', () => setWake(wakeBox.checked));
      checkDevice().then(() => {
        showPrivacy();
        // Resume listening after a reload only where it was agreed to: on this computer, or online with consent.
        if (stored(WAKE_KEY, false) && (onDevice || stored(CLOUD_OK_KEY, false))) {
          wakeBox.checked = true; fab.classList.add('is-awake'); startWake();
        }
      });
    }

    post('assistant', opts.greeting || 'Hi, I am Parsec. Ask me about your deals: "open Acme", "what\'s wrong with the customer contract", "next problem", "not a problem", "full screen" or "export".');
    return { ask, post, open: () => toggle(true) };
  }

  root.FinePrintVoice = { mount };
})(window);
