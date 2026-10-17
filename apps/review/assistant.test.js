// Run with: node --test apps/review/assistant.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const { parseCommand, bestMatch } = require('./assistant.js');

const CTX = {
  deals: ['Acme Cloud', 'Beta Foods', 'F_practice_data_room'],
  documents: ['1_cim_acme.txt', '2_management_accounts.txt', '3_customer_arr.txt', 'Contracts/Northwind MSA.pdf'],
  categories: [
    { id: 'financial', name: 'Financial' }, { id: 'legal', name: 'Legal and contracts' },
    { id: 'tax', name: 'Tax' }, { id: 'people', name: 'People and HR' },
    { id: 'tech', name: 'Technology and security' }, { id: 'commercial', name: 'Customers and market' },
  ],
};
const p = (text) => parseCommand(text, CTX);

test('opening a deal folder by name, however it is said', () => {
  for (const said of ['open Acme', 'open the acme cloud folder', 'Please analyse the Acme deal', 'check beta foods']) {
    const c = p(said);
    assert.equal(c.action, 'open-deal', said);
  }
  assert.equal(p('open Acme').deal, 'Acme Cloud');
  assert.equal(p('check beta foods').deal, 'Beta Foods');
});

test('asking what is wrong with a document names that document', () => {
  const c = p("what's wrong with the customer contract?");
  assert.equal(c.action, 'problems');
  assert.equal(c.document, '3_customer_arr.txt');
  assert.equal(p('any problems in the management accounts').document, '2_management_accounts.txt');
  assert.equal(p('what are the issues with the Northwind MSA').document, 'Contracts/Northwind MSA.pdf');
});

test('asking about a document in a named folder opens the folder too', () => {
  const c = p('open acme and tell me what is wrong with the cim');
  assert.equal(c.action, 'problems');
  assert.equal(c.deal, 'Acme Cloud');
  assert.equal(c.document, '1_cim_acme.txt');
});

test('what is wrong, with nothing named, is a summary of the whole deal', () => {
  const c = p("what's wrong here");
  assert.equal(c.action, 'problems');
  assert.equal(c.document, undefined);
});

test('moving between problems and removing a false mark', () => {
  assert.equal(p('next problem').action, 'next');
  assert.equal(p('next').action, 'next');
  assert.equal(p('go back').action, 'previous');
  for (const said of ["that's not a problem", 'remove this mark', 'false alarm', 'dismiss it']) {
    assert.equal(p(said).action, 'dismiss', said);
  }
  assert.equal(p('bring it back').action, 'restore');
  assert.equal(p('confirm this one').action, 'confirm');
});

test('export, list, areas, summary, stop and help', () => {
  assert.equal(p('export the marked documents').action, 'export');
  assert.equal(p('save marked copies').action, 'export');
  assert.equal(p('what folders do I have').action, 'list-deals');
  const area = p('show me the tax problems');
  assert.equal(area.action, 'area');
  assert.equal(area.category, 'tax');
  assert.equal(p('show HR issues').category, 'people');
  assert.equal(p('show all problems').category, 'all');
  assert.equal(p('give me a summary').action, 'summary');
  assert.equal(p('stop').action, 'stop');
  assert.equal(p('what can you do').action, 'help');
});

test('"list" or "show" inside a question is not an area filter', () => {
  const c = parseCommand("what's wrong with the customer list in acme", { deals: ['Acme Cloud', 'Latch merger'] });
  assert.equal(c.action, 'problems');
  assert.equal(c.deal, 'Acme Cloud');
  assert.equal(p('list the tax problems').action, 'area');
});

test('a real question goes to the AI, with the phrase match kept as a fallback guess', () => {
  const c = p('which of these problems would hurt the price the most and why?');
  assert.equal(c.action, 'unknown');
  assert.equal(c.guess.action, 'problems');
  assert.equal(p('why is the customer list a problem').action, 'unknown');
  assert.equal(p("what's wrong with the customer contract").action, 'problems');   // a plain request stays instant
  assert.equal(p('how bad is it').action, 'summary');
});

test('"Hey Parsec" wakes the assistant, however the browser spells what it heard', () => {
  const { wake } = require('./assistant.js');
  assert.deepEqual(wake('Hey Parsec, open Acme'), { woke: true, rest: 'open Acme' });
  assert.deepEqual(wake('hey parsec'), { woke: true, rest: '' });
  for (const heard of ['ok parsek show the findings', 'hey par sec next problem', 'hi pass sec export', 'Parsec zoom in']) {
    assert.equal(wake(heard).woke, true, heard);
  }
  assert.equal(wake('hey parsec next problem').rest, 'next problem');
  assert.equal(wake('the parties agree to the terms').woke, false);
  assert.equal(wake('').woke, false);
});

test('screen commands: full screen, zoom, scroll, views, home, print, read, close', () => {
  assert.equal(p('i want this in full screen').action, 'fullscreen');
  assert.equal(p('make it full screen').action, 'fullscreen');
  assert.equal(p('exit full screen').action, 'exit-fullscreen');
  assert.equal(p('normal view').action, 'exit-fullscreen');
  assert.equal(p('zoom in').action, 'zoom-in');
  assert.equal(p('make the text bigger').action, 'zoom-in');
  assert.equal(p('zoom out').action, 'zoom-out');
  assert.equal(p('scroll down').action, 'scroll-down');
  assert.equal(p('page up').action, 'scroll-up');
  assert.deepEqual(p('show the findings'), { action: 'view', view: 'findings' });
  assert.deepEqual(p('open the document view'), { action: 'view', view: 'document' });
  assert.deepEqual(p('go to the questions'), { action: 'view', view: 'followups' });
  assert.equal(p('go home').action, 'go-home');
  assert.equal(p('back to my deals').action, 'go-home');
  assert.equal(p('print this').action, 'print');
  assert.equal(p('read that again').action, 'read');
  assert.equal(p('close the assistant').action, 'close');
});

test('"export" and "print" act only as instructions, not when heard in the middle of a sentence', () => {
  assert.notEqual(p('we should not export anything yet').action, 'export');
  assert.notEqual(p('they will print the report later').action, 'print');
  assert.equal(p('please export').action, 'export');
  assert.equal(p('print it').action, 'print');
});

test('something it does not understand is said to be unknown, not guessed', () => {
  assert.equal(p('make me a sandwich').action, 'unknown');
  assert.equal(p('').action, 'unknown');
});

test('bestMatch ignores filler words and file extensions, and needs a real overlap', () => {
  assert.equal(bestMatch('the customer file', CTX.documents), '3_customer_arr.txt');
  assert.equal(bestMatch('the document', CTX.documents), null);
  assert.equal(bestMatch('zebra', CTX.deals), null);
});
