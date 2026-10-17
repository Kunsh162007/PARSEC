/*
 * The visitor's demo code. Deals they upload, and their review and marked copies of every deal, are kept under it:
 * the same code on any device shows the same work. A new visitor is given a random code; they can also type their
 * own, or open a link ending in ?demo=CODE. The server stores a digest of the code, never the code itself.
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'fineprint-visitor';
  const ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';   // no I, L, O or U, so a code reads back without mistakes
  const VALID = /^[A-Z0-9_-]{12,64}$/;

  const normalise = (code) => String(code || '').trim().toUpperCase().replace(/\s+/g, '-');
  const isValid = (code) => VALID.test(normalise(code));

  function fresh() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    const chars = Array.from(bytes, (b) => ALPHABET[b % ALPHABET.length]).join('');
    return `PARSEC-${chars.match(/.{4}/g).join('-')}`;
  }

  let key = '';
  function remember(code) {
    key = code;
    try { localStorage.setItem(STORAGE_KEY, code); } catch (err) { /* storage blocked: kept for this page only */ }
  }

  function set(code) {
    const clean = normalise(code);
    if (!VALID.test(clean)) return null;
    remember(clean);
    return clean;
  }

  try { key = normalise(localStorage.getItem(STORAGE_KEY)); } catch (err) { key = ''; }
  if (!VALID.test(key)) remember(fresh());

  const url = new URL(window.location.href);
  if (url.searchParams.has('demo')) {
    set(url.searchParams.get('demo'));
    url.searchParams.delete('demo');
    history.replaceState(null, '', url.pathname + url.search + url.hash);
  }

  window.FinePrintVisitor = { key: () => key, set, fresh, isValid };
})();
