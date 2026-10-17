/*
 * A PDF shown as its original pages, with the marks drawn over the words they point at.
 *
 * When the page reads a PDF it records, for every run of text, where it sits on its page and which characters of
 * the extracted text it holds (app.js, readPdf). A finding's span is a character range in that text, so its boxes
 * are the parts of the runs it covers. Pages are drawn with pdf.js only when they scroll into view; the boxes are
 * placed at once, so "show in document" can scroll to a mark before its page is drawn.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.FinePrintPdfView = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const MAX_PAGE_WIDTH = 900;   // CSS pixels at 100% zoom
  const RENDER_AHEAD = '800px'; // start drawing a page this far before it scrolls into view

  const supports = (layout) => Boolean(layout && Array.isArray(layout.pages) && layout.pages.length
    && layout.pages.every((p) => !((p.rotate || 0) % 360)));

  // The boxes covering characters [start, end) of the text, in PDF units from each page's top-left corner.
  // With the text and a measure(string, font) of letter widths, a box starts and ends at the real letters; without
  // them every letter of a run is taken to be the same width.
  function rects(layout, span, text, measure) {
    const out = [];
    const share = (it, chars) => {
      const length = it.end - it.start;
      const n = Math.max(0, Math.min(length, chars));
      if (!text || !measure) return n / length;
      const whole = measure(text.slice(it.start, it.end), it.font);
      return whole > 0 ? measure(text.slice(it.start, it.start + n), it.font) / whole : n / length;
    };
    layout.pages.forEach((page, index) => {
      const [x0, , , y1] = page.view;
      for (const it of page.items) {
        if (it.end <= span.start || it.start >= span.end || it.end <= it.start) continue;
        const from = share(it, span.start - it.start);
        const to = share(it, span.end - it.start);
        out.push({
          page: index,
          left: it.x - x0 + it.w * from,
          width: Math.max(1, it.w * (to - from)),
          top: y1 - it.y - it.h * 0.88,
          height: it.h * 1.18,
        });
      }
    });
    return out;
  }

  /* ---------------------------------------------------------------- */
  /* drawing (browser only)                                            */
  /* ---------------------------------------------------------------- */

  const drawn = new WeakMap();   // layout -> { width, pages: Map(page -> canvas) }: re-renders reuse drawn pages
  const KEEP_PAGES = 24;         // drawn pages kept per document; older ones are drawn again if scrolled back to

  // Letter widths in the run's font family (pdf.js names a family for each font: serif, sans-serif, monospace or
  // the font's own name), so a box starts and ends at the right letters in proportional type.
  let measurer = null;
  function measure(str, family) {
    if (!measurer) measurer = document.createElement('canvas').getContext('2d');
    measurer.font = `100px ${family || 'sans-serif'}`;
    return measurer.measureText(str).width;
  }

  function documentFor(layout, loadPdfjs) {
    if (!layout.doc) {
      layout.doc = loadPdfjs().then((lib) => lib.getDocument({ data: layout.bytes.slice(0) }).promise)
        .catch((err) => { layout.doc = null; throw err; });   // a failed load can be tried again
    }
    return layout.doc;
  }

  async function drawPage(layout, index, cssWidth, holder, loadPdfjs) {
    const width = Math.round(cssWidth);
    let entry = drawn.get(layout);
    if (!entry || entry.width !== width) {   // a new zoom: pages drawn at the old size are let go
      entry = { width, pages: new Map() };
      drawn.set(layout, entry);
    }
    const cache = entry.pages;
    const key = index;
    let canvas = cache.get(key);
    if (canvas) { cache.delete(key); cache.set(key, canvas); }   // most recently used last
    if (!canvas) {
      const pdf = await documentFor(layout, loadPdfjs);
      const page = await pdf.getPage(index + 1);
      const ratio = window.devicePixelRatio || 1;
      const viewport = page.getViewport({ scale: (cssWidth / (page.view[2] - page.view[0])) * ratio });
      canvas = document.createElement('canvas');
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      canvas.setAttribute('aria-hidden', 'true');
      await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
      cache.set(key, canvas);
      while (cache.size > KEEP_PAGES) cache.delete(cache.keys().next().value);
    }
    holder.replaceChildren(canvas);
  }

  // Draws every page of `layout` into `sheet`, with a <mark> per box (the page's click and selection code works on
  // these exactly as on marks in text). opts: spans [{start, end, f}], zoom, loadPdfjs, title(f), isCross(f), isActive(f)
  function render(sheet, opts) {
    const { layout } = opts;
    const available = Math.max(280, (sheet.clientWidth || MAX_PAGE_WIDTH) - 28);
    const width = Math.min(MAX_PAGE_WIDTH, available) * (opts.zoom || 1);
    const container = document.createElement('div');
    container.className = 'pdf-pages';
    const holders = [];
    layout.pages.forEach((page, index) => {
      const scale = width / (page.view[2] - page.view[0]);
      const div = document.createElement('div');
      div.className = 'pdf-page';
      div.style.width = `${width}px`;
      div.style.height = `${(page.view[3] - page.view[1]) * scale}px`;
      div.dataset.page = String(index);
      const holder = document.createElement('div');
      holder.className = 'pdf-canvas';
      holder.style.cssText = 'position:absolute;inset:0';
      const num = document.createElement('span');
      num.className = 'pdf-num';
      num.textContent = `Page ${index + 1} of ${layout.pages.length}`;
      div.append(holder, num);
      container.append(div);
      holders.push({ div, holder, scale, index });
    });
    for (const { start, end, f } of opts.spans) {
      for (const r of rects(layout, { start, end }, opts.text, opts.text ? measure : null)) {
        const { div, scale } = holders[r.page];
        const mark = document.createElement('mark');
        mark.className = `pdf-mark sev-${f.severity}${opts.isCross(f) ? ' is-cross' : ''}${opts.isActive(f) ? ' is-active' : ''}`;
        mark.dataset.ids = f.id;
        mark.dataset.a = String(start);
        mark.dataset.b = String(end);
        mark.tabIndex = 0;
        mark.title = opts.title(f);
        mark.style.cssText = `left:${r.left * scale - 1}px;top:${r.top * scale}px;width:${r.width * scale + 2}px;height:${r.height * scale}px`;
        div.append(mark);
      }
    }
    sheet.replaceChildren(container);
    const draw = (h) => drawPage(layout, h.index, width, h.holder, opts.loadPdfjs).catch(() => {
      h.holder.textContent = 'This page could not be drawn.';
    });
    if (!('IntersectionObserver' in window)) { holders.forEach(draw); return; }
    const watcher = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        watcher.unobserve(e.target);
        draw(holders[Number(e.target.dataset.page)]);
      });
    }, { root: null, rootMargin: RENDER_AHEAD });
    holders.forEach((h) => watcher.observe(h.div));
  }

  return { supports, rects, render };
});
