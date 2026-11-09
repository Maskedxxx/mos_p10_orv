'use strict';
const assert = require('node:assert/strict');
const { chromium } = require('playwright-core');
const { browserState, search, readPage, readBlock } = require('/app/p10/garant_web.cjs');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:19222');
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    const fixture = `<html><body><div id="documentInfinityViewer-1" style="height:120px;overflow:auto">
      <div data-page="1" status="loaded" style="height:180px"><p>Первая часть.</p></div>
      <div data-page="2" status="loaded" style="height:180px"><p>Вторая часть.</p></div>
      <div data-page="3" status="loaded" style="height:180px"><p>Третья часть.</p></div>
      <div data-page="4" status="loaded" style="height:180px"><p>Четвёртая часть.</p></div>
      <div data-page="5" status="extended" style="height:180px"><p>Пятая часть.</p>
        <div data-relativeblockid="2000"><table><tr><td>Объединённая запись</td> <td>42</td></tr></table></div></div>
      <div data-page="6" status="collapsed" style="height:1px"></div></div>
      <script>window.fixtureHtml='<div data-relativeblockid="2000"><table><tr><td>Объединённая запись</td> <td>42</td></tr></table></div>';
      window.Ext={getCmp:()=>({documentId:1234,pageCount:6,isDocumentReady:true,getPageText:()=>window.fixtureHtml})}</script></body></html>`;
    await page.route('https://internet.garant.ru/**', route => route.fulfill({
      status: 200, contentType: 'text/html; charset=utf-8', body: fixture,
    }));
    await page.goto('https://internet.garant.ru/#/document/1234');
    const state = await browserState(page, '1234');
    assert.deepEqual(state.loaded, [1, 2, 3, 4, 5, 6]);
    assert.equal(state.covered[6], 5);
    const found = await search(page, { topic: '1234', query: 'Объединённая запись', start_page: 1, match_offset: 0 }, state);
    assert.deepEqual(found.pages_scanned, [1, 2, 3, 4, 5, 6]);
    assert.equal(found.matches.length, 1);
    assert.deepEqual(found.matches[0].pages, [5, 6]);
    assert.equal(found.end_of_document, true);
    const fromCollapsed = await search(page, { topic: '1234', query: 'Объединённая запись', start_page: 6, match_offset: 0 }, state);
    assert.deepEqual(fromCollapsed.pages_scanned, [5, 6]);
    assert.equal(fromCollapsed.matches.length, 1);
    const read = await readPage(page, { topic: '1234', page: 6, offset: 0 }, state);
    assert.equal(read.covered_by, 5);
    assert.match(read.content, /Объединённая запись/);
    const block = await readBlock(page, { topic: '1234', entry: '2000', offset: 0 }, { ...state, located: [5] });
    assert.deepEqual(block.pages, [5, 6]);
    assert.match(block.content, /Часть 6 объединена/);
    await page.evaluate(() => { window.fixtureHtml = window.fixtureHtml.replace('<td>42</td>', '<td colspan="2">42</td>'); });
    assert.ok(!(await browserState(page, '1234')).loaded.includes(6));
    console.log('Объединённые части и граница поиска: OK');
  } finally { await context.close(); }
  process.stdout.write('', () => process.exit(0));
})().catch(error => { console.error(error.stack); process.exit(1); });
