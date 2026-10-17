/*
 * A random key for this browser, kept across visits. Deals uploaded with it belong to this browser: on a shared demo,
 * only this browser lists, opens or removes them. The server stores a digest of it, never the key itself.
 */
(function () {
  'use strict';

  const STORAGE_KEY = 'fineprint-visitor';
  const VALID = /^[A-Za-z0-9_-]{16,128}$/;

  function fresh() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('');
  }

  let key = '';
  try {
    key = localStorage.getItem(STORAGE_KEY) || '';
    if (!VALID.test(key)) {
      key = fresh();
      localStorage.setItem(STORAGE_KEY, key);
    }
  } catch (err) {
    key = fresh();   // storage blocked: the key lasts for this page only
  }

  window.FinePrintVisitor = { key: () => key };
})();
