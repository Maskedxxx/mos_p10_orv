'use strict';

// Чтение авторизованной страницы Гаранта; stdout содержит только JSON ответа.
const readline = require('readline');
const { createHash } = require('crypto');

const CHUNK = 6000;
const BATCH = 5;
const MAX_MATCHES = 20;
const CONTEXT = 220;

class ToolError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

function validate(input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new ToolError('invalid_input', 'Нужен объект параметров.');
  if (!['block', 'page', 'search'].includes(input.action)) throw new ToolError('invalid_input', 'Неизвестное действие.');
  if (typeof input.topic !== 'string' || !/^[0-9]{1,12}$/.test(input.topic)) throw new ToolError('invalid_input', 'Укажите числовой идентификатор документа.');
  const integer = (name, fallback, min, max) => {
    const value = input[name] === undefined ? fallback : input[name];
    if (!Number.isSafeInteger(value) || value < min || value > max) throw new ToolError('invalid_input', `Неверный параметр ${name}.`);
    return value;
  };
  const expectedHash = () => {
    if (input.expected_hash === undefined) return null;
    if (typeof input.expected_hash !== 'string' || !/^[a-f0-9]{64}$/.test(input.expected_hash)) throw new ToolError('invalid_input', 'Неверный контрольный отпечаток текста.');
    return input.expected_hash;
  };
  if (input.action === 'block') {
    if (typeof input.entry !== 'string' || !/^[0-9]{1,12}$/.test(input.entry)) throw new ToolError('invalid_input', 'Укажите числовой идентификатор блока.');
    return { action: input.action, topic: input.topic, entry: input.entry, offset: integer('offset', 0, 0, 10000000), expected_hash: expectedHash() };
  }
  if (input.action === 'page') return { action: input.action, topic: input.topic, page: integer('page', 1, 1, 100000), offset: integer('offset', 0, 0, 10000000), expected_hash: expectedHash() };
  if (typeof input.query !== 'string' || !input.query.trim() || input.query.length > 250) throw new ToolError('invalid_input', 'Нужна непустая фраза длиной до 250 символов.');
  return { action: input.action, topic: input.topic, query: input.query.trim(), start_page: integer('start_page', 1, 1, 100000), match_offset: integer('match_offset', 0, 0, 1000000), expected_hash: expectedHash() };
}

function escaped(text) { return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

// Совпадение между соседними частями возвращается один раз, с номерами обеих частей.
function matchesInPage(text, previous, number, query) {
  const carry = previous.slice(-Math.max(CONTEXT, query.length + CONTEXT));
  const window = carry + (carry ? '\n' : '') + text;
  const boundary = carry.length + (carry ? 1 : 0);
  const regex = new RegExp(query.split(/\s+/u).map(escaped).join('\\s+'), 'giu');
  const found = [];
  for (const match of window.matchAll(regex)) {
    const start = match.index;
    const end = start + match[0].length;
    if (end <= boundary) continue;
    found.push({
      pages: start < boundary ? [number - 1, number] : [number],
      position: start < boundary ? previous.length - carry.length + start : start - boundary,
      excerpt: window.slice(Math.max(0, start - CONTEXT), Math.min(window.length, end + CONTEXT)),
      context_continues: end + CONTEXT > window.length,
    });
  }
  return found;
}

function chunk(text, offset, expectedHash = null) {
  const hash = createHash('sha256').update(text).digest('hex');
  if (offset && !expectedHash) throw new ToolError('hash_required', 'Для продолжения передайте content_hash предыдущего ответа.');
  if (expectedHash && hash !== expectedHash) throw new ToolError('content_changed', 'Текст изменился после предыдущего ответа; начните чтение заново.');
  const chars = Array.from(text);
  if (offset > chars.length) throw new ToolError('invalid_offset', 'Смещение больше длины текста.');
  const content = chars.slice(offset, offset + CHUNK).join('');
  const end = offset + Array.from(content).length;
  return { content, offset, total_chars: chars.length, content_hash: hash, next_offset: end < chars.length ? end : null };
}

async function bounded(promise, ms = 8000) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new ToolError('timeout', 'Браузер не ответил вовремя.')), ms);
    })]);
  } finally { clearTimeout(timer); }
}

async function browserState(page, topic) {
  return bounded(page.evaluate(topic => {
    const pages = [...document.querySelectorAll('[data-page]')].filter(e => e.getClientRects().length);
    const covered = {};
    const ready = (e, index) => {
      const viewer = e.parentElement;
      const component = viewer && window.Ext?.getCmp?.(viewer.id);
      const confirmed = component?.isDocumentReady === true && String(component.documentId) === topic
        && component.pageCount === pages.length;
      const status = e.getAttribute('status');
      if (status === 'loaded') {
        const emptyEnd = viewer?.id.startsWith('documentInfinityViewer-') && confirmed
          && Number(e.getAttribute('data-page')) === pages.length && e.children.length === 1
          && e.firstElementChild?.matches('p.empty') && e.firstElementChild.childElementCount === 0
          && !e.innerText.trim();
        return (e.children.length > 0 && !!e.innerText.trim()) || emptyEnd;
      }
      if (status === 'extended') return !!confirmed && e.children.length > 0 && !!e.innerText.trim();
      if (status === 'collapsed' && confirmed && viewer?.id.startsWith('documentInfinityViewer-')) {
        let leader = index - 1;
        while (leader >= 0 && pages[leader].getAttribute('status') === 'collapsed') leader--;
        if (leader < 0 || pages[leader].getAttribute('status') !== 'extended') return false;
        try {
          const html = component.getPageText(Number(e.getAttribute('data-page')));
          if (typeof html !== 'string' || !html) return false;
          const doc = new DOMParser().parseFromString(html, 'text/html');
          if (doc.body.querySelector('img,svg,canvas')) return false;
          const norm = text => text.replace(/\s+/gu, ' ').trim();
          const body = doc.body.cloneNode(true);
          body.querySelectorAll('table').forEach(table => table.remove());
          const text = norm(body.textContent);
          if (text && !norm(pages[leader].innerText).includes(text)) return false;
          const cells = row => [...row.querySelectorAll(':scope > td,:scope > th')].map(cell =>
            [norm(cell.textContent), cell.rowSpan, cell.colSpan]);
          const rows = [...pages[leader].querySelectorAll('tr')].map(row => JSON.stringify(cells(row)));
          const sourceRows = [...doc.body.querySelectorAll('tr')];
          if (!text && !sourceRows.length) return false;
          if (sourceRows.some(row => !rows.includes(JSON.stringify(cells(row))))) return false;
          covered[Number(e.getAttribute('data-page'))] = Number(pages[leader].getAttribute('data-page'));
          return true;
        } catch { return false; }
      }
      return viewer?.id.startsWith('documentLightViewer-') && confirmed
        && e.children.length > 0 && !!e.innerText.trim();
    };
    const hash = decodeURIComponent(location.hash);
    return { host: location.hostname, protocol: location.protocol, hash, title: document.title, count: pages.length,
      login: location.hostname === 'account.garant.ru',
      loaded: pages.filter(ready).map(e => Number(e.getAttribute('data-page'))), covered };
  }, topic));
}

async function openDocument(page, input) {
  const entry = input.action === 'block' ? `/entry/${input.entry}` : '';
  try {
    await page.goto(`https://internet.garant.ru/#/document/${input.topic}${entry}`, { waitUntil: 'domcontentloaded', timeout: 30000 });
  } catch { /* При переходе на судебный раздел исходная навигация может прерваться. */ }
  for (let attempt = 0; attempt < 60; attempt++) {
    await page.waitForTimeout(500);
    let state;
    try { state = await browserState(page, input.topic); }
    catch (error) { if (error instanceof ToolError) throw error; else continue; }
    if (state.login) throw new ToolError('auth_required', 'Гарант запросил повторный вход в учётную запись.');
    if (state.protocol !== 'https:' || !['internet.garant.ru', 'arbitr.garant.ru'].includes(state.host)) throw new ToolError('open_failed', 'Гарант перенаправил на страницу ошибки или доступ недоступен.');
    const match = state.hash.match(/^#\/document\/(\d+)(?:[/:]|$)/);
    if (match && match[1] === input.topic && state.count && state.loaded.length) {
      if (input.action !== 'block') return state;
      const located = await bounded(page.evaluate(({ entryId, loaded }) => {
        const pages = [...document.querySelectorAll('[data-page]')].filter(e => e.getClientRects().length);
        return pages.filter(e => loaded.includes(Number(e.getAttribute('data-page'))) && [...e.querySelectorAll('[data-relativeblockid]')].some(x => x.getAttribute('data-relativeblockid') === entryId)).map(e => Number(e.getAttribute('data-page')));
      }, { entryId: input.entry, loaded: state.loaded }));
      if (located.length) return { ...state, located };
    }
  }
  if (input.action === 'block') throw new ToolError('block_unverified', 'Не удалось открыть указанный блок; его наличие не установлено.');
  throw new ToolError('open_incomplete', 'Документ не загрузился; его наличие и содержание не установлены.');
}

async function loadPart(page, number, topic, total) {
  for (let attempt = 0; attempt < 20; attempt++) {
    const state = await browserState(page, topic);
    if (state.login) throw new ToolError('auth_required', 'Гарант запросил повторный вход.');
    if (state.protocol !== 'https:' || !['internet.garant.ru', 'arbitr.garant.ru'].includes(state.host)) throw new ToolError('open_failed', 'Страница покинула разрешённые разделы Гаранта.');
    const match = state.hash.match(/^#\/document\/(\d+)(?:[/:]|$)/);
    if (!match || match[1] !== topic || state.count !== total) throw new ToolError('document_changed', 'Документ или число его частей изменились во время чтения.');
    const found = await bounded(page.evaluate(n => {
      const pages = [...document.querySelectorAll('[data-page]')].filter(e => e.getClientRects().length);
      const target = pages.find(e => Number(e.getAttribute('data-page')) === n);
      if (!target) return false;
      target.scrollIntoView({ block: 'start' });
      target.parentElement.dispatchEvent(new Event('scroll'));
      return true;
    }, number));
    if (!found) throw new ToolError('part_missing', `Не обнаружена часть ${number}.`);
    await page.waitForTimeout(500);
    const current = await browserState(page, topic);
    if (current.loaded.includes(number)) return;
  }
  throw new ToolError('part_incomplete', `Часть ${number} не загрузилась; её содержание не проверено.`);
}

async function extract(page, number, entry = null, query = null, topic = null) {
  const state = await browserState(page, topic);
  if (!state.loaded.includes(number)) return null;
  const coveredBy = state.covered[number] || null;
  return bounded(page.evaluate(({ number, entry, query, topic, coveredBy }) => {
    const pages = [...document.querySelectorAll('[data-page]')].filter(e => e.getClientRects().length);
    const p = pages.find(e => Number(e.getAttribute('data-page')) === number);
    if (!p) return null;
    if (coveredBy) {
      const leader = pages.find(e => Number(e.getAttribute('data-page')) === coveredBy);
      if (p.getAttribute('status') !== 'collapsed' || leader?.getAttribute('status') !== 'extended') return null;
      if (entry !== null) {
        const component = window.Ext?.getCmp?.(p.parentElement.id);
        const html = component?.getPageText?.(number);
        if (typeof html !== 'string') return { incomplete: true };
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const raw = [...doc.querySelectorAll('[data-relativeblockid]')].filter(e => e.getAttribute('data-relativeblockid') === entry);
        if (!raw.length) return { exists: false };
        const actual = [...leader.querySelectorAll('[data-relativeblockid]')].filter(e => e.getAttribute('data-relativeblockid') === entry);
        const norm = text => text.replace(/\s+/gu, ' ').trim();
        if (!actual.some(e => raw.every(r => norm(r.textContent) && norm(e.textContent).includes(norm(r.textContent))))) return { incomplete: true };
      }
      return { exists: true, covered_by: coveredBy, text: '', tables: [], image_count: 0, candidates: [] };
    }
    const viewer = p.parentElement;
    const component = viewer && window.Ext?.getCmp?.(viewer.id);
    const count = [...document.querySelectorAll('[data-page]')].filter(e => e.getClientRects().length).length;
    const confirmed = component?.isDocumentReady === true && String(component.documentId) === topic
      && component.pageCount === count;
    const emptyEnd = viewer?.id.startsWith('documentInfinityViewer-') && confirmed
      && p.getAttribute('status') === 'loaded' && number === count && p.children.length === 1
      && p.firstElementChild?.matches('p.empty') && p.firstElementChild.childElementCount === 0
      && !p.innerText.trim();
    if (emptyEnd) return entry === null ? { exists: true, empty: true, text: '', tables: [], image_count: 0, candidates: [] } : { exists: false };
    const lightReady = viewer?.id.startsWith('documentLightViewer-') && confirmed
      && p.children.length > 0 && !!p.innerText.trim();
    const extendedReady = p.getAttribute('status') === 'extended' && confirmed && !!p.innerText.trim();
    if ((p.getAttribute('status') !== 'loaded' && !lightReady && !extendedReady) || !p.innerText.trim()) return null;
    let root = p;
    if (entry !== null) {
      const matches = [...p.querySelectorAll('[data-relativeblockid]')].filter(e => e.getAttribute('data-relativeblockid') === entry);
      const top = matches.filter(e => !matches.some(other => other !== e && other.contains(e)));
      if (!top.length) return { exists: false };
      if (top.length !== 1) return { ambiguous: true };
      root = top[0];
      if (!root.innerText.trim()) return { incomplete: true };
    }
    const tables = [...root.querySelectorAll('table')].map((table, i) => ({
      number: i + 1,
      rows: [...table.querySelectorAll('tr')].map(row => [...row.querySelectorAll(':scope > td, :scope > th')].map(cell => ({
        text: cell.innerText.trim(), rowspan: cell.rowSpan, colspan: cell.colSpan,
      }))),
    }));
    const candidates = [];
    if (query) {
      const escape = text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const phrase = new RegExp(query.split(/\s+/u).map(escape).join('\\s+'), 'iu');
      for (const el of root.querySelectorAll('p, td, li')) {
        if (!phrase.test(el.innerText)) continue;
        const block = el.closest('[data-relativeblockid]');
        const id = block?.getAttribute('data-relativeblockid') || null;
        if (id && !candidates.includes(id)) candidates.push(id);
      }
    }
    return { exists: true, text: root.innerText, tables, image_count: root.querySelectorAll('img').length, candidates };
  }, { number, entry, query, topic, coveredBy }));
}

function rendered(parts) {
  const warnings = [];
  const text = parts.map(({ number, data }) => {
    let out = data.empty ? `--- Часть ${number}: пустая служебная страница ---`
      : data.covered_by ? `--- Часть ${number} объединена Гарантом с частью ${data.covered_by} ---`
      : `--- Часть ${number} ---\n${data.text}`;
    if (data.tables.length) out += '\n\nСтруктура таблиц этой части (строки и ячейки):\n' + JSON.stringify(data.tables);
    if (data.image_count) warnings.push(`В части ${number} обнаружено изображений: ${data.image_count}; их содержимое не извлечено.`);
    return out;
  }).join('\n\n');
  return { text, warnings };
}

async function readBlock(page, input, state) {
  const start = state.located[0];
  const parts = [];
  await loadPart(page, start, input.topic, state.count);
  const first = await extract(page, start, input.entry, null, input.topic);
  if (first?.incomplete) throw new ToolError('part_incomplete', `Блок в части ${start} не содержит прочитанного текста.`);
  if (!first || !first.exists || first.ambiguous) throw new ToolError('block_unverified', 'Нужный блок не удалось однозначно прочитать.');
  parts.push({ number: start, data: first });
  for (const direction of [-1, 1]) {
    for (let n = start + direction; n >= 1 && n <= state.count; n += direction) {
      await loadPart(page, n, input.topic, state.count);
      const data = await extract(page, n, input.entry, null, input.topic);
      if (!data) throw new ToolError('part_incomplete', `Часть ${n} не прочитана.`);
      if (data.incomplete) throw new ToolError('part_incomplete', `Блок в части ${n} не содержит прочитанного текста.`);
      if (data.ambiguous) throw new ToolError('block_unverified', 'Несколько блоков с одинаковым идентификатором; текст не выбран.');
      if (!data.exists) break;
      parts.push({ number: n, data });
    }
  }
  parts.sort((a, b) => a.number - b.number);
  const output = rendered(parts);
  return { ok: true, action: 'block', topic: input.topic, entry: input.entry, title: state.title,
    source: `https://${state.host}/#/document/${input.topic}/entry/${input.entry}`,
    pages: parts.map(x => x.number), warnings: output.warnings, ...chunk(output.text, input.offset, input.expected_hash) };
}

async function readPage(page, input, state) {
  if (input.page > state.count) throw new ToolError('invalid_page', `У документа ${state.count} частей.`);
  await loadPart(page, input.page, input.topic, state.count);
  let data = await extract(page, input.page, null, null, input.topic);
  if (!data) throw new ToolError('part_incomplete', 'Часть не прочитана.');
  const coveredBy = data.covered_by;
  if (coveredBy) {
    data = await extract(page, coveredBy, null, null, input.topic);
    if (!data) throw new ToolError('part_incomplete', 'Объединённая часть не прочитана.');
  }
  const output = rendered([{ number: coveredBy || input.page, data }]);
  if (coveredBy) output.warnings.unshift(`Часть ${input.page} объединена Гарантом с частью ${coveredBy}; возвращён объединённый текст.`);
  return { ok: true, action: 'page', topic: input.topic, title: state.title,
    source: `https://${state.host}/#/document/${input.topic}`, page: input.page, pages_total: state.count,
    covered_by: coveredBy || null, empty: !!data.empty, warnings: output.warnings, ...chunk(output.text, input.offset, input.expected_hash) };
}

async function search(page, input, state) {
  if (input.start_page > state.count) throw new ToolError('invalid_page', `У документа ${state.count} частей.`);
  await loadPart(page, input.start_page, input.topic, state.count);
  let current = await browserState(page, input.topic);
  const start = current.covered[input.start_page] || input.start_page;
  let last = Math.min(state.count, start + BATCH - 1);
  let first = Math.max(1, start - 1);
  if (first < start) {
    await loadPart(page, first, input.topic, state.count);
    current = await browserState(page, input.topic);
    first = current.covered[first] || first;
  }
  let previous = '';
  let failed = null;
  const found = [];
  const candidates = [];
  const scanned = [];
  const warnings = [];
  const batchHash = createHash('sha256');
  // Часть перед диапазоном нужна для поиска на границе; объединённые части читаются вместе.
  for (let n = first; n <= last; n++) {
    try {
      await loadPart(page, n, input.topic, state.count);
      current = await browserState(page, input.topic);
      const data = await extract(page, n, null, input.query, input.topic);
      if (!data) throw new ToolError('part_incomplete', `Часть ${n} не прочитана.`);
      batchHash.update(`${n}\0${data.text.length}\0${data.text}`);
      if (data.covered_by) {
        if (n >= start) scanned.push(n);
        continue;
      }
      const covered = Object.entries(current.covered).filter(([, leader]) => leader === n).map(([part]) => Number(part));
      if (n >= start && covered.length) last = Math.max(last, ...covered);
      if (n >= start) {
        const hits = matchesInPage(data.text, previous, n, input.query);
        for (const hit of hits) hit.pages = [...new Set([...hit.pages, ...covered])].sort((a, b) => a - b);
        found.push(...hits);
        if (data.candidates.length) candidates.push({ page: n, entries: data.candidates.slice(0, 20), truncated: data.candidates.length > 20 });
        if (data.image_count) warnings.push({ page: n, images: data.image_count, message: 'Содержимое изображений не извлечено и не участвовало в поиске.' });
        scanned.push(n);
      }
      previous = data.text;
    } catch (error) {
      if (!(error instanceof ToolError)) throw error;
      failed = { page: n, code: error.code, message: error.message };
      break;
    }
  }
  const contentHash = batchHash.digest('hex');
  if (input.match_offset && !input.expected_hash) throw new ToolError('hash_required', 'Для продолжения совпадений передайте expected_hash из next.');
  if (input.expected_hash && input.expected_hash !== contentHash) throw new ToolError('content_changed', 'Текст изменился во время поиска; начните диапазон заново.');
  if (input.match_offset > found.length) throw new ToolError('invalid_offset', 'Смещение совпадений больше их количества.');
  const selected = found.slice(input.match_offset, input.match_offset + MAX_MATCHES);
  let next = null;
  if (input.match_offset + selected.length < found.length) next = { start_page: start, match_offset: input.match_offset + selected.length, expected_hash: contentHash };
  else if (failed) next = { start_page: failed.page, match_offset: 0 };
  else if (last < state.count) next = { start_page: last + 1, match_offset: 0 };
  return { ok: true, action: 'search', topic: input.topic, title: state.title,
    source: `https://${state.host}/#/document/${input.topic}`, query: input.query,
    pages_total: state.count, pages_scanned: scanned, matches_in_batch: found.length,
    matches: selected, candidates, warnings, failed, next, end_of_document: !failed && !next && last === state.count };
}

let activeCleanup = null;

async function run(raw) {
  const input = validate(raw);
  const { chromium } = require('playwright-core');
  let browser;
  try { browser = await chromium.connectOverCDP('http://127.0.0.1:19222', { timeout: 5000 }); }
  catch { throw new ToolError('browser_unavailable', 'Браузер Гаранта недоступен.'); }
  const context = browser.contexts()[0];
  const page = await bounded(context.newPage());
  let cleanupPromise;
  let browserSession, targetId;
  activeCleanup = () => {
    if (!cleanupPromise) cleanupPromise = (async () => {
      try {
        if (browserSession && targetId) await bounded(browserSession.send('Target.closeTarget', { targetId }), 3000);
        else await bounded(page.close(), 3000);
      } catch { /* Процесс не должен зависнуть из-за закрытия собственной вкладки. */ }
    })();
    return cleanupPromise;
  };
  try {
    const tabSession = await bounded(context.newCDPSession(page));
    targetId = (await bounded(tabSession.send('Target.getTargetInfo'))).targetInfo.targetId;
    browserSession = await bounded(browser.newBrowserCDPSession());
    const state = await openDocument(page, input);
    if (input.action === 'block') return await readBlock(page, input, state);
    if (input.action === 'page') return await readPage(page, input, state);
    return await search(page, input, state);
  } finally {
    await activeCleanup();
    activeCleanup = null;
  }
}

if (require.main === module) {
  process.once('SIGTERM', async () => {
    if (activeCleanup) await activeCleanup();
    process.exit(124);
  });
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.once('line', async line => {
    let reply;
    try { reply = await run(JSON.parse(line)); }
    catch (error) {
      reply = { ok: false, error: { code: error instanceof ToolError ? error.code : 'internal_error',
        message: error instanceof ToolError ? error.message : 'Не удалось обработать запрос к браузеру.' } };
    }
    process.stdout.write(JSON.stringify(reply) + '\n', () => process.exit(0));
  });
}

module.exports = { ToolError, validate, matchesInPage, chunk, rendered, search, openDocument, browserState, readPage, readBlock };
