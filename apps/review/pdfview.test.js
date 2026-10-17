// Run with: node --test apps/review/pdfview.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const PV = require('./pdfview.js');

// Two lines on one page: "0123456789" at y=700 and "abcdefghij" at y=680, each 100 units wide, 12 high.
const LAYOUT = {
  pages: [{
    view: [0, 0, 600, 800], rotate: 0,
    items: [
      { start: 0, end: 10, x: 100, y: 700, w: 100, h: 12 },
      { start: 12, end: 22, x: 100, y: 680, w: 100, h: 12 },
    ],
  }],
};

test('a span inside one text run becomes one box over just those characters', () => {
  const [r] = PV.rects(LAYOUT, { start: 2, end: 5 });
  assert.equal(r.page, 0);
  assert.equal(r.left, 120);
  assert.equal(r.width, 30);
  assert.ok(r.top < 800 - 700 && r.top > 800 - 700 - 12);   // above the baseline, measured from the top
});

test('a span across two lines becomes one box per line', () => {
  const rs = PV.rects(LAYOUT, { start: 5, end: 15 });
  assert.equal(rs.length, 2);
  assert.equal(rs[0].left, 150);
  assert.equal(rs[0].width, 50);
  assert.equal(rs[1].left, 100);
  assert.equal(rs[1].width, 30);
  assert.ok(rs[1].top > rs[0].top);
});

test('with the real letter widths, a box covers exactly its letters, not an even share of the run', () => {
  // "iiiiiWWWWW": five narrow letters (1 unit) then five wide ones (9 units); the run is 100 units wide in all.
  const text = 'iiiiiWWWWW';
  const layout = { pages: [{ view: [0, 0, 600, 800], rotate: 0, items: [{ start: 0, end: 10, x: 100, y: 700, w: 100, h: 12, font: 'serif' }] }] };
  const measure = (s) => [...s].reduce((sum, ch) => sum + (ch === 'i' ? 1 : 9), 0);
  const [r] = PV.rects(layout, { start: 5, end: 10 }, text, measure);
  assert.equal(r.left, 110);    // after 5 narrow letters: 5 of 50 units, so 10% of the run
  assert.equal(r.width, 90);
  const [even] = PV.rects(layout, { start: 5, end: 10 });   // without widths: halfway
  assert.equal(even.left, 150);
});

test('a span outside every run has no box', () => {
  assert.deepEqual(PV.rects(LAYOUT, { start: 10, end: 12 }), []);
});

test('rotated pages are not drawn as pages (the text view is used instead)', () => {
  assert.equal(PV.supports(LAYOUT), true);
  assert.equal(PV.supports({ pages: [{ view: [0, 0, 1, 1], rotate: 90, items: [] }] }), false);
  assert.equal(PV.supports(null), false);
});
