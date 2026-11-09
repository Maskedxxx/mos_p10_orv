'use strict';
const assert = require('node:assert/strict');
const { chromium } = require('playwright-core');
const { search } = require('/app/p10/garant_web.cjs');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:19222');
  const page = await browser.contexts()[0].newPage();
  try {
    const fixture = `<div style="height:120px;overflow:auto">
      <div data-page="1" status="loaded" style="height:200px"><p>Первый пункт: искомый срок.</p><img alt="Схема" src="data:image/gif;base64,R0lGODlhAQABAIAAAAUEBA=="></div>
      <div data-page="2" status="loaded" style="height:200px"><p>Второй пункт.</p></div>
      <div data-page="3" style="height:200px"></div>
    </div>`;
    await page.route('https://internet.garant.ru/**', route => route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: fixture }));
    await page.goto('https://internet.garant.ru/#/document/70166342', { waitUntil: 'domcontentloaded' });
    const state = { count: 3, title: 'Тестовый документ' };
    for (const query of ['искомый срок', 'несуществующая фраза']) {
      const result = await search(page, { topic: '70166342', query, start_page: 1, match_offset: 0 }, state);
      assert.deepEqual(result.pages_scanned, [1, 2]);
      assert.equal(result.failed.page, 3);
      assert.equal(result.failed.code, 'part_incomplete');
      assert.deepEqual(result.next, { start_page: 3, match_offset: 0 });
      assert.equal(result.end_of_document, false);
      assert.equal(result.warnings[0].page, 1);
      assert.match(result.warnings[0].message, /не участвовало в поиске/);
      assert.equal(result.matches.length, query === 'искомый срок' ? 1 : 0);
    }
    console.log('2 проверки неполной загрузки: OK');
  } finally { await page.close(); }
  process.stdout.write('', () => process.exit(0));
})().catch(error => { console.error(error.message); process.exit(1); });
