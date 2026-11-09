'use strict';
const assert = require('node:assert/strict');
const { chromium } = require('playwright-core');
const { openDocument } = require('/app/p10/garant_web.cjs');

(async () => {
  const browser = await chromium.connectOverCDP('http://127.0.0.1:19222');
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await page.route('https://internet.garant.ru/**', route => route.fulfill({
      status: 302, headers: { location: 'https://account.garant.ru/login' }, body: '',
    }));
    await page.route('https://account.garant.ru/**', route => route.fulfill({
      status: 200, contentType: 'text/html; charset=utf-8', body: '<h1>Вход в систему ГАРАНТ</h1>',
    }));
    await assert.rejects(openDocument(page, { action: 'page', topic: '409114710' }), error => error.code === 'auth_required');
    console.log('Повторный вход распознан: OK');
  } finally { await context.close(); }
  process.stdout.write('', () => process.exit(0));
})().catch(error => { console.error(error.message); process.exit(1); });
