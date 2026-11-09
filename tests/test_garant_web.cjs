'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { validate, matchesInPage, chunk, rendered } = require('../scripts/garant_web.cjs');

test('параметры не позволяют подменить адрес или передать ошибочные смещения', () => {
  assert.throws(() => validate({ action: 'block', topic: '../../other', entry: '1' }));
  assert.throws(() => validate({ action: 'block', topic: '12', entry: '1/../../' }));
  assert.throws(() => validate({ action: 'page', topic: '12', page: 0 }));
  assert.throws(() => validate({ action: 'search', topic: '12', query: ' ' }));
  assert.deepEqual(validate({ action: 'search', topic: '12', query: 'срок' }),
    { action: 'search', topic: '12', query: 'срок', start_page: 1, match_offset: 0, expected_hash: null });
});

test('поиск понимает спецсимволы как текст и находит фразу на границе один раз', () => {
  assert.equal(matchesInPage('Пункт 4.6 действует', '', 1, '4.6').length, 1);
  assert.equal(matchesInPage('Пункт 4x6 действует', '', 1, '4.6').length, 0);
  assert.equal(matchesInPage('НОРМА', '', 1, 'норма').length, 1);
  assert.equal(matchesInPage('В случае\nнеполучения', '', 1, 'в случае неполучения').length, 1);
  const hits = matchesInPage('новый пункт', 'конец', 2, 'конец новый');
  assert.equal(hits.length, 1);
  assert.deepEqual(hits[0].pages, [1, 2]);
  assert.equal(matchesInPage('нет', 'найдено', 2, 'найдено').length, 0);
});

test('длинный ответ читается по частям без разрыва Unicode', () => {
  const text = 'а'.repeat(5999) + '😀' + 'б'.repeat(10);
  const a = chunk(text, 0);
  const b = chunk(text, a.next_offset, a.content_hash);
  assert.equal(a.content + b.content, text);
  assert.equal(b.next_offset, null);
  assert.throws(() => chunk(text, 1));
  assert.throws(() => chunk(text + 'x', a.next_offset, a.content_hash));
  assert.throws(() => chunk(text, 7000, a.content_hash));
});

test('таблица сохраняет ячейки и объединения, изображения не объявляются прочитанными', () => {
  const data = { text: 'Текст', image_count: 1, tables: [{ number: 1, rows: [[
    { text: 'Название', rowspan: 2, colspan: 1 }, { text: 'Значение', rowspan: 1, colspan: 2 },
  ]] }] };
  const result = rendered([{ number: 5, data }]);
  assert.match(result.text, /"rowspan":2/);
  assert.match(result.text, /"colspan":2/);
  assert.match(result.warnings[0], /не извлечено/);
});
