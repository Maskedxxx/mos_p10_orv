'use strict';
const assert = require('node:assert/strict');
const { chromium } = require('playwright-core');
const { browserState, openDocument } = require('/app/p10/garant_web.cjs');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:19222');
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    const make = id => `<html><body><div id="documentLightViewer-1"><div data-page="1">
      <div data-relativeblockid="101"><p>Содержимое документа.</p></div></div></div>
      <script>if(location.hostname==='arbitr.garant.ru' && !location.hash) history.replaceState(null,'','#/document/'+${id});
      window.Ext={getCmp:()=>window.fixtureComponent};
      window.fixtureComponent={documentId:${id},pageCount:1,isDocumentReady:true}</script></body></html>`;
    await page.route('https://internet.garant.ru/**', route => route.fulfill({
      status: 200, contentType: 'text/html; charset=utf-8', body: make(1234),
    }));
    await page.goto('https://internet.garant.ru/#/document/1234');
    assert.deepEqual((await browserState(page, '1234')).loaded, [1]);
    await page.evaluate(() => { window.fixtureComponent.isDocumentReady = false; });
    assert.deepEqual((await browserState(page, '1234')).loaded, []);
    await page.evaluate(() => { window.fixtureComponent.isDocumentReady = true; window.fixtureComponent.documentId = 999; });
    assert.deepEqual((await browserState(page, '1234')).loaded, []);
    await page.evaluate(() => { window.fixtureComponent.documentId = 1234; window.fixtureComponent.pageCount = 2; });
    assert.deepEqual((await browserState(page, '1234')).loaded, []);
    await page.evaluate(() => { window.fixtureComponent.pageCount = 1; });
    await page.evaluate(() => {
      const root = document.querySelector('[data-page]');
      root.dataset.saved = root.innerHTML;
      root.innerHTML = '';
      root.setAttribute('status', 'loaded');
    });
    assert.deepEqual((await browserState(page, '1234')).loaded, []);
    await page.evaluate(() => {
      const root = document.querySelector('[data-page]');
      root.innerHTML = root.dataset.saved;
      root.removeAttribute('status');
    });
    console.log('Проверка блока на light-странице');
    const state = await openDocument(page, { action: 'block', topic: '1234', entry: '101' });
    assert.deepEqual(state.located, [1]);

    console.log('Блок прочитан, проверка перехода в судебный раздел');
    const courtPage = await context.newPage();
    await courtPage.route('https://internet.garant.ru/**', route => route.fulfill({
      status: 302, headers: { location: 'https://arbitr.garant.ru/#/document/350092802' }, body: '',
    }));
    await courtPage.route('https://arbitr.garant.ru/**', route => route.fulfill({
      status: 200, contentType: 'text/html; charset=utf-8', body: make(350092802),
    }));
    const court = await openDocument(courtPage, { action: 'page', topic: '350092802' });
    assert.equal(court.host, 'arbitr.garant.ru');
    assert.deepEqual(court.loaded, [1]);

    const endPage = await context.newPage();
    await endPage.route('https://internet.garant.ru/**', route => route.fulfill({
      status: 200, contentType: 'text/html; charset=utf-8', body: `<html><body>
      <div id="documentInfinityViewer-1"><div data-page="1" status="loaded"><p>Текст</p></div>
      <div data-page="2" status="loaded"><p class="empty">&nbsp;</p></div></div>
      <script>window.fixtureComponent={documentId:1234,pageCount:2,isDocumentReady:true};
      window.Ext={getCmp:()=>window.fixtureComponent}</script></body></html>`,
    }));
    await endPage.goto('https://internet.garant.ru/#/document/1234');
    assert.deepEqual((await browserState(endPage, '1234')).loaded, [1, 2]);
    await endPage.evaluate(() => { window.fixtureComponent.documentId = 999; });
    assert.deepEqual((await browserState(endPage, '1234')).loaded, [1]);
    await endPage.evaluate(() => {
      window.fixtureComponent.documentId = 1234;
      document.querySelector('[data-page="2"] p').innerHTML = '<img alt="Изображение">';
    });
    assert.deepEqual((await browserState(endPage, '1234')).loaded, [1]);
    console.log('Типы страниц, судебный раздел и проверенная пустая последняя часть: OK');
  } finally { await context.close(); }
  process.stdout.write('', () => process.exit(0));
})().catch(error => { console.error(error.message); process.exit(1); });
