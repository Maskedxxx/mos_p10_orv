"""
Согласованность промптов v2 с контрактами (шаг 5 пула внедрения, 2026-09-04).

Промпты индикаторов говорят агенту «смотри documents.state», «status.effective_date»
и т. п. — эти ключи обязаны существовать в схеме паспорта, а значения «…» после
знака «=» — быть допустимыми значениями перечислений. Примеры JSON в блоках
<формат_ответа> промптов 04a/04b/05/06 обязаны проходить свои контракты: пример —
это образец для модели, невалидный образец учит невалидному ответу.

Раньше эти проверки гонялись ручными скриптами; здесь они закреплены как тест.
"""

from __future__ import annotations

import json
import re

import pytest

from app.config import (
    PROMPT_HYPOTHESES_NO_DATA,
    PROMPT_HYPOTHESES_VIOLATED,
    PROMPT_MERGE,
    PROMPT_PASSPORT,
    PROMPT_DIALOG,
    PROMPT_REQUEST,
    PROMPTS_DIR,
    all_indicators,
    prompt_path,
)
from app.contracts import ActPassport, DataRequest, DialogTurn, HypothesisMap, HypothesisSet, HypothesisSetOutput, MergeOutput

INDICATOR_PROMPTS = [prompt_path(ind.number) for ind in all_indicators()]
# 13.10 (разбор узлов): индикаторы со своим контрактом — правила в описаниях полей, промпт только направляет
from app.nodes.verdicts import INDICATOR_OUTPUT  # noqa: E402
RUBRIC_INDICATORS = set(INDICATOR_OUTPUT)
OLD_STYLE_INDICATOR_PROMPTS = [prompt_path(n) for n in range(1, 13) if n not in RUBRIC_INDICATORS]
NODE_PROMPTS = [PROMPT_HYPOTHESES_VIOLATED, PROMPT_HYPOTHESES_NO_DATA, PROMPT_MERGE, PROMPT_REQUEST, PROMPT_DIALOG]
PASSPORT_ROOTS = "requisites|status|addressee|nature|requirements|procedure|documents|deadlines|references|allocation|control"
KEY_RE = re.compile(r"\b(?:" + PASSPORT_ROOTS + r")(?:\.[a-z_]+)+")
VALUE_RE = re.compile(r"(?:" + PASSPORT_ROOTS + r")(?:\.[a-z_]+)+\s*=\s*«([^»]+)»")
TAG_OPEN_RE = re.compile(r"^<([а-яa-z_]+)>\s*$", re.MULTILINE)
TAG_CLOSE_RE = re.compile(r"^</([а-яa-z_]+)>\s*$", re.MULTILINE)


def _passport_paths_and_enums() -> tuple[set[str], set[str]]:
    """Все пути полей вида a.b.c и все значения перечислений из схемы ActPassport."""
    schema = ActPassport.model_json_schema()
    defs = schema.get("$defs", {})

    def resolve(node):
        if "$ref" in node:
            return defs[node["$ref"].split("/")[-1]]
        if "anyOf" in node:
            for a in node["anyOf"]:
                if a.get("type") != "null":
                    return resolve(a)
        return node

    paths: set[str] = set()
    enums: set[str] = set()

    def walk(node, prefix):
        node = resolve(node)
        enums.update(node.get("enum", []))
        if node.get("type") == "array":
            walk(node["items"], prefix)
            return
        for key, value in node.get("properties", {}).items():
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            walk(value, path)

    walk(schema, "")
    for d in defs.values():
        enums.update(d.get("enum", []))
    return paths, enums


PASSPORT_PATHS, PASSPORT_ENUMS = _passport_paths_and_enums()


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def test_prompts_dir_has_exactly_the_expected_files():
    """20 промптов, включая отдельный режим дополнения: паспорт + 12 индикаторов + 04a, 04b, 05, 06, помощник 07, Гарант-помощник 08. Саммари больше нет."""
    names = sorted(p.name for p in PROMPTS_DIR.glob("*.md"))
    expected = sorted(
        ["01_passport.md", "04a_hypotheses.md", "04b_hypotheses.md", "05_merge.md", "06_request.md", "06_request_append.md", "07_dialog.md",
         "08_garant_chat.md"]
        + [f"02_indicator_{n:02d}.md" for n in range(1, 13)]
    )
    assert names == expected


@pytest.mark.parametrize("path", INDICATOR_PROMPTS + NODE_PROMPTS + [PROMPT_PASSPORT], ids=lambda p: p.name)
def test_xml_tags_are_balanced(path):
    text = _read(path)
    assert sorted(TAG_OPEN_RE.findall(text)) == sorted(TAG_CLOSE_RE.findall(text))


@pytest.mark.parametrize("path", INDICATOR_PROMPTS, ids=lambda p: p.name)
def test_indicator_prompt_describes_passport_input(path):
    if path in [prompt_path(n) for n in RUBRIC_INDICATORS]:
        pytest.skip("рубрика в контракте: блока «паспорт» в промпте нет (13.10)")
    """Каждый индикатор знает, где лежит паспорт и как читать его состояния."""
    text = _read(path)
    assert "## ПАСПОРТ АКТА" in text
    assert "<паспорт>" in text and "</паспорт>" in text
    assert "«не понял»" in text
    # букв-псевдонимов полей (поле A, B, C…) в промптах v2 нет — только ключи JSON
    assert not re.search(r"пол[еяьюи]м?\s+[ABCDFGH]\b", text)


@pytest.mark.parametrize("path", INDICATOR_PROMPTS + NODE_PROMPTS, ids=lambda p: p.name)
def test_every_passport_key_in_prompt_exists_in_schema(path):
    text = _read(path)
    used = {m.group().rstrip(".") for m in KEY_RE.finditer(text)}
    missing = sorted(k for k in used if k not in PASSPORT_PATHS)
    assert missing == [], f"{path.name}: ключей нет в схеме паспорта: {missing}"


@pytest.mark.parametrize("path", INDICATOR_PROMPTS + NODE_PROMPTS, ids=lambda p: p.name)
def test_every_quoted_value_after_key_is_a_known_enum(path):
    text = _read(path)
    bad = sorted({v for v in VALUE_RE.findall(text) if v not in PASSPORT_ENUMS})
    assert bad == [], f"{path.name}: значений нет среди перечислений паспорта: {bad}"


def _example_json(text: str, start_marker: str, end_marker: str) -> dict:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return json.loads(text[start:end].rstrip())


@pytest.mark.parametrize("path, model_cls, start, end", [
    (PROMPT_DIALOG, DialogTurn, '{\n  "reply"', "\n\nПоле plan"),
], ids=["07"])
def test_example_in_prompt_validates_against_contract(path, model_cls, start, end):
    """Образец ответа в промпте обязан проходить контракт узла — иначе он учит
    модель невалидному ответу, который runner отвергнет."""
    model_cls.model_validate(_example_json(_read(path), start, end))


@pytest.mark.parametrize("name", ["04a_hypotheses.md", "04b_hypotheses.md", "05_merge.md"])
def test_hypothesis_prompts_mention_expected_trend(name):
    """Правило передаётся в схеме, без обязательного повторения в промпте."""
    from app.contracts import EffectOutput
    rule = EffectOutput.model_fields["expected_trend"].description
    assert "Критерий проверки" in rule and "сам по себе не доказывает" in rule
    assert "Направление явно (рост или снижение)" not in _read(PROMPTS_DIR / name)


def test_passport_prompt_does_not_mention_summary():
    assert "саммари" not in _read(PROMPT_PASSPORT).lower()


def test_no_prompt_mentions_summary_as_input():
    """Саммари удалено из пайплайна; ни один промпт не должен ждать его на входе."""
    for path in INDICATOR_PROMPTS + NODE_PROMPTS:
        text = _read(path).lower()
        assert "саммари" not in text, path.name


def test_request_prompt_knows_existing_blank_block():
    from app.nodes.request import EXISTING_BLANK_HEADER
    from app.config import PROMPT_REQUEST_APPEND
    initial = PROMPT_REQUEST.read_text(encoding="utf-8")
    append = PROMPT_REQUEST_APPEND.read_text(encoding="utf-8")
    assert EXISTING_BLANK_HEADER not in initial
    assert EXISTING_BLANK_HEADER in append
    assert "breakdown" in append and "why_dropped" in append



# ---- 2.5: выжимка для эксперта — отдельный ключ рядом с полным текстом ----

_BRIEF_OPEN, _BRIEF_CLOSE = "<выжимка_для_эксперта>", "</выжимка_для_эксперта>"


def _brief_block(path) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.count(_BRIEF_OPEN) == 1 and text.count(_BRIEF_CLOSE) == 1, path.name
    return text[text.index(_BRIEF_OPEN):text.index(_BRIEF_CLOSE)]


def test_indicator_contracts_share_one_brief_rule():
    """Правило выжимки одно на все 12 индикаторов. 14.10: у всех свои контракты — правило в описании поля
    rationale_brief, одинаковом у всех; в промптах блока выжимки нет."""
    descriptions = {cls.model_fields["rationale_brief"].description for cls in INDICATOR_OUTPUT.values()}
    assert len(descriptions) == 1
    rule = descriptions.pop()
    assert "Смотрел:" in rule and "Не нашёл:" in rule
    assert RUBRIC_INDICATORS == set(range(1, 13))


def test_hypotheses_have_branch_specific_briefs_and_no_few_shot():
    from app.contracts import HypothesisOutput, NoDataHypothesisOutput, EffectOutput
    for path in (PROMPT_HYPOTHESES_VIOLATED, PROMPT_HYPOTHESES_NO_DATA):
        text = _read(path)
        assert "<example>" not in text and '"hypotheses":' not in text
        assert "оценка эксперта" not in text and "прошлая версия" not in text.lower()
    assert HypothesisOutput.model_fields["hypothesis_brief"].description != NoDataHypothesisOutput.model_fields["hypothesis_brief"].description
    assert "не пиши установленное нарушение" in NoDataHypothesisOutput.model_fields["hypothesis_brief"].description
    assert "Динамика не обязательна" in EffectOutput.model_fields["expected_trend_brief"].description


def test_merge_prompt_says_how_to_brief_merged_rows():
    text = PROMPT_MERGE.read_text(encoding="utf-8")
    assert "Для слитой строки составь общую формулировку и согласованные выжимки" in text
    assert "<example>" not in text and '\"rows\":' not in text
    assert "overlap" in text and "accounting_note" in text
    assert "Кандидаты Python помогают проверке, но не ограничивают её" in text


# ---- 2.7 (итерация 4): промпты индикаторов по разбору обратной связи В. Д. Чуракова ----
# Буквы — доработки из 4_документы/План_реализации_итерации_4.md. Убранная фраза
# должна исчезнуть, новая — появиться; так тест держит правку от случайного отката.

_ITER4_REMOVED = {
    1: ["Оригинал документа, если закон прямо требует именно оригинал",            # Н
        "Межведомственного взаимодействия там быть не может"],                       # О
    2: ["не объяснено технической невозможностью",                                  # П
        "Бумажный оригинал документа, если он требуется однократно"],                # Р
    3: ["У органа-разработчика статус чужих актов не спрашивают",                   # Т
        "Отсутствие в ссылке номера последней редакции"],                            # С
    4: ["Федеральный акт можно привести как дополнительный ориентир"],               # Ф
    6: ["relation = «устанавливает» или «переносит»",                               # Х
        "Приказ утверждает «Руководство по соблюдению обязательных требований»",    # Ц
        "Указание, что акт действует до принятия нового акта",                      # Ч
        "Интернета у тебя нет, поэтому НЕ утверждай",                               # Ч, Р5
        "Срок действия установлен другим актом, на который есть ссылка"],           # М
    7: ["Частота, прямо установленная федеральной нормой или обоснованная"],        # Щ
    8: ["вынужден получать в другом органе",                                        # Э
        "Документ, который адресат составляет сам: заявление"],                      # Я
    9: ["канцелярит",                                                               # АА
        "Формулировка допускает два толкования, и из текста нельзя понять",         # АБ
        "Сроки в акте заданы в разных единицах измерения без объяснения"],           # АГ
    10: ["публичные обсуждения результатов контрольной деятельности"],              # АВ
    11: ["предмет индикатора 5, не твой",                                           # АЕ
        "Сроки исполнения обязанностей сами по себе основанием"],                   # АД
    12: ["для установки одного объекта требуют опыт на десятках"],                   # АЗ
}

_ITER4_ADDED = {
    1: ["required_from_applicant", "coverage_gaps"],   # О — 13.10 в контракте
    2: ["электронно, но обязательна бумага"],   # Р — 13.10 в контракте
    3: ["Ссылка на акт без указания редакции"],
    4: ["Двойной сбор документов в эту проверку не включай"],   # У — 13.10 в контракте
    5: ["исправить в той же заявке"],   # 13.10 — рубрика в контракте
    6: ["247-ФЗ", "устанавливает сам", "дата вступления в силу"],   # 13.10 — рубрика в контракте
    7: ["явный повтор"],   # Ш — 13.10 в контракте: «повторно проходит то же согласование», «второй этап»
    8: ["повторяет другой"],   # Я — 13.10 в контракте: «отдельное согласие, когда оно уже дано в заявлении»
    9: ["существенная неопределённость", "Два прочтения не обязательны"],   # АБ — 13.10 в контракте
    10: ["targets", "coverage", "только если акт"],
    11: ["срок подачи заявки", "срок исполнения обязанности", "Различие единиц само по себе не доказывает"],   # АД, АЕ, АГ — 14.10 в контракте
    12: ["за свой счёт", "у указанного актом исполнителя"],   # АЗ — 14.10: требование к опыту — не нарушение (оценка), в контракте
}


@pytest.mark.parametrize("n", range(1, 13))
def test_iter4_indicator_prompt_edits(n):
    text = prompt_path(n).read_text(encoding="utf-8")
    if n in RUBRIC_INDICATORS:   # 13.10: правила — в описаниях полей контракта индикатора
        text += json.dumps(INDICATOR_OUTPUT[n].model_json_schema(), ensure_ascii=False)
    for phrase in _ITER4_REMOVED.get(n, []):
        assert phrase not in text, f"промпт {n}: осталась убранная фраза «{phrase}»"
    for phrase in _ITER4_ADDED.get(n, []):
        assert phrase in text, f"промпт {n}: нет новой фразы «{phrase}»"
    # Т: хвост про missing_data — для аналитика, а не «что запрашивать у органа»
    assert "иначе непонятно, что запрашивать у органа" not in text
    # Ю: агент знает только свои границы — ни номера чужого индикатора, ни старого блока
    others = {int(m) for m in re.findall(r"[Ии]ндикатор[а-яё]*\s*(?:№\s*)?(\d+)", text)} - {n}
    assert not others, f"промпт {n}: упомянуты индикаторы {sorted(others)}"
    assert "<границы_с_соседними_индикаторами>" not in text


@pytest.mark.parametrize("path", INDICATOR_PROMPTS + NODE_PROMPTS + [PROMPT_PASSPORT], ids=lambda p: p.name)
def test_region_labels_only_in_comparison_prompts(path):
    """К: меток регионов нет ни в одном промпте (7, 8, 11 — с 13–14.10 без сравнения с регионами); после подстановки меток не остаётся."""
    from app.nodes.verdicts import fill_prompt_labels
    text = path.read_text(encoding="utf-8")
    has = "{{РЕГИОНЫ_СРАВНЕНИЯ}}" in text or "{{ЧИСЛО_РЕГИОНОВ}}" in text
    assert not has
    assert "{{" not in fill_prompt_labels(text, limit=5)   # потолок подставляется всегда (узел вердиктов)


def test_hypotheses_data_rules_preserved_in_schema():
    from app.contracts import NumberItemOutput, OrganQuestion, EffectOutput
    rule = NumberItemOutput.model_fields["source"].description
    assert "число жалоб и судебных споров по применению акта" in rule
    assert "официальное значение" in rule and "доли или коэффициента" in rule
    assert "source" in NumberItemOutput.model_fields
    assert "числовое измерение" in OrganQuestion.model_fields["question"].description
    how = EffectOutput.model_fields["how_to_calculate"].description
    assert "прошивка (только если акт её требует)" in how
    assert "не добавляй обязательную поездку без основания" in how


def test_iter4_request_prompt():
    from app.contracts import RequestItem
    text = PROMPT_REQUEST.read_text(encoding="utf-8")
    assert "<example>" not in text and '\"items\":' not in text
    assert "отдельные позиции с подстроками" in text
    assert "Похожие, но разные показатели" in text
    assert "официально учитываемая доля/коэффициент" in RequestItem.model_fields["metric"].description
    assert "Одна числовая ячейка — одна величина" in RequestItem.model_fields["metric"].description
    assert "более узкое измерение не удаляй" in RequestItem.model_fields["note"].description


def test_iter4_dialog_prompt():
    """Е, АИ, Ж2, Ж3, В в промпте диалога; операция «нигде не ведётся» есть в схеме."""
    from app.contracts import MarkNotTracked
    text = " ".join(PROMPT_DIALOG.read_text(encoding="utf-8").split())
    assert "бизнес, граждане и город" in text and "без процентов и долей" not in text
    assert "«пометить: нигде не ведётся»" in text
    assert MarkNotTracked.model_fields["kind"].annotation.__args__[0] == "пометить: нигде не ведётся"
    assert "эксперт подсказал, где искать" in text and "«Вопросы органу»" in text


def test_dialog_prompt_assistant_tools():
    """Помощник (14.10): вход — карта прогона и служебная строка, источники и справка по Гаранту (все инструменты,
    лимиты, выгрузку делает сам и предупреждает), режим ПОИСК; блоков, которые заменила карта прогона, нет."""
    from app.config import GARANT_ALL_TOOLS
    text = " ".join(PROMPT_DIALOG.read_text(encoding="utf-8").split())
    for phrase in ("«# КАРТА ПРОГОНА»", "«## СЛУЖЕБНО»", "<источники>", "делай это в этом же ходе",
                   "garant_entry_text", "Если выгрузил — скажи в ответе", "2. ПОИСК", "перечитай нужные файлы"):
        assert phrase in text, phrase
    for tool in ("garant_search", "garant_document_redactions", "garant_snippets", "garant_sutyazhnik",
                 "garant_export_block_html", "garant_export_html"):
        assert tool in text and tool in GARANT_ALL_TOOLS, tool
    for gone in ("## ВЕРДИКТЫ (JSON)", "## КАРТА ГИПОТЕЗ (JSON)", "Интернета у тебя нет", "во входе",
                 "Разрешить выгрузку", "пятнадцать форм"):
        assert gone not in text, gone



def test_iter4_request_prompt_mandatory_block():
    text = PROMPT_REQUEST.read_text(encoding="utf-8")
    assert "## ОБЯЗАТЕЛЬНЫЕ ПОКАЗАТЕЛИ (JSON)" in text
    assert "Среднее из обязательного блока не заменяй счётчиком" in text
    assert "со всеми кодами" in text
    from app.nodes.request import MANDATORY_HEADER
    assert MANDATORY_HEADER == "## ОБЯЗАТЕЛЬНЫЕ ПОКАЗАТЕЛИ (JSON)"


def test_iter4_developer_in_prompts():
    """А: гипотезы, запрос и индикатор 10 берут разработчика из requisites.developers; паспорт ищет его в Гаранте."""
    for path in (PROMPT_HYPOTHESES_VIOLATED, PROMPT_HYPOTHESES_NO_DATA, PROMPT_REQUEST, prompt_path(10)):
        text = path.read_text(encoding="utf-8")
        assert "requisites.developers" in text, path.name
    # 13.10 (рубрика паспорта): правила паспорта — в описаниях полей контракта, промпт только направляет
    from app.contracts import Requisites
    developers = Requisites.model_fields["developers"].description
    assert "Number(<номер>) & Date(<дата>;<дата>)" in developers
    assert "Correspondents(<topic> 0)" in developers
    assert "garant" not in PROMPT_PASSPORT.read_text(encoding="utf-8")



def test_iter4_garant_in_indicator_prompts():
    """Л, М: у агентов с Гарантом — блок правовой базы с их инструментами; потолок — меткой; у 3 нет групп риска."""
    from app.config import INDICATOR_GARANT_TOOLS
    from app.nodes.verdicts import fill_prompt_labels
    for n, tools in INDICATOR_GARANT_TOOLS.items():
        text = prompt_path(n).read_text(encoding="utf-8")
        block = text.split("<правовая_база>")[1].split("</правовая_база>")[0]
        for t in tools:
            assert t in block, (n, t)
        named = set(re.findall(r"garant_[a-z_]+", text))
        allowed_names = {t.split("__")[-1] for t in tools}
        assert named <= allowed_names, (n, named - allowed_names)   # агент не зовёт то, чего у него нет
        assert "{{" not in fill_prompt_labels(text, limit=7)
    for n in (3, 4, 10, 11):
        assert "{{ПОТОЛОК_ЗАХОДОВ}} ЗАХОДОВ ПОИСКА В ИНТЕРНЕТЕ" in prompt_path(n).read_text(encoding="utf-8"), n
    t3 = prompt_path(3).read_text(encoding="utf-8").lower()
    assert "высокий риск" not in t3 and "низкий риск" not in t3 and "garant_find_hyperlinks" in t3
    assert "<правовая_база>" not in prompt_path(6).read_text(encoding="utf-8")


@pytest.mark.parametrize("n", sorted(RUBRIC_INDICATORS))
def test_rubric_indicator_prompt_only_points_to_contract(n):
    """13.10: промпт индикатора с рубрикой в контракте — роль, суть, вход, инструменты; правил,
    примеров и образца JSON в нём нет, модель заполняет ответ по описаниям полей схемы."""
    text = prompt_path(n).read_text(encoding="utf-8")
    assert "## ПОЛНЫЙ ТЕКСТ АКТА" in text and "## ПАСПОРТ АКТА" in text
    assert "строго по описаниям полей схемы" in " ".join(text.split())
    assert "<example>" not in text and "ФОРМАТ ОТВЕТА — строго JSON" not in text
    assert "<паспорт>" not in text and "152-ПП" not in text

