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
    """17 промптов: паспорт + 12 индикаторов + 04a, 04b, 05, 06. Саммари больше нет."""
    names = sorted(p.name for p in PROMPTS_DIR.glob("*.md"))
    expected = sorted(
        ["01_passport.md", "04a_hypotheses.md", "04b_hypotheses.md", "05_merge.md", "06_request.md", "07_dialog.md"]
        + [f"02_indicator_{n:02d}.md" for n in range(1, 13)]
    )
    assert names == expected


@pytest.mark.parametrize("path", INDICATOR_PROMPTS + NODE_PROMPTS + [PROMPT_PASSPORT], ids=lambda p: p.name)
def test_xml_tags_are_balanced(path):
    text = _read(path)
    assert sorted(TAG_OPEN_RE.findall(text)) == sorted(TAG_CLOSE_RE.findall(text))


@pytest.mark.parametrize("path", INDICATOR_PROMPTS, ids=lambda p: p.name)
def test_indicator_prompt_describes_passport_input(path):
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
    (PROMPT_HYPOTHESES_VIOLATED, HypothesisSetOutput, '{\n  "indicator": 7', "\n\nПоле grounds"),
    (PROMPT_HYPOTHESES_NO_DATA, HypothesisSetOutput, '{\n  "indicator": 10', "\n\nПоле where"),
    (PROMPT_MERGE, MergeOutput, '{\n  "rows"', "\n\nПоле decisions"),
    (PROMPT_REQUEST, DataRequest, '{\n  "items"', "\n\nПоле years"),
    (PROMPT_DIALOG, DialogTurn, '{\n  "reply"', "\n\nПоле plan"),
], ids=["04a", "04b", "05", "06", "07"])
def test_example_in_prompt_validates_against_contract(path, model_cls, start, end):
    """Образец ответа в промпте обязан проходить контракт узла — иначе он учит
    модель невалидному ответу, который runner отвергнет."""
    model_cls.model_validate(_example_json(_read(path), start, end))


@pytest.mark.parametrize("name", ["04a_hypotheses.md", "04b_hypotheses.md", "05_merge.md"])
def test_hypothesis_prompts_mention_expected_trend(name):
    """Пул 3, п. 1б: ключ expected_trend контракта Effect назван в правилах и примерах промптов гипотез и слияния."""
    text = _read(PROMPTS_DIR / name)
    assert text.count("expected_trend") >= 2, name


def test_passport_prompt_does_not_mention_summary():
    assert "саммари" not in _read(PROMPT_PASSPORT).lower()


def test_no_prompt_mentions_summary_as_input():
    """Саммари удалено из пайплайна; ни один промпт не должен ждать его на входе."""
    for path in INDICATOR_PROMPTS + NODE_PROMPTS:
        text = _read(path).lower()
        assert "саммари" not in text, path.name


def test_request_prompt_knows_existing_blank_block():
    """Промпт 06 описывает блок текущего бланка ровно под тем заголовком, что шлёт код (шаг 1.4)."""
    from app.nodes.request import EXISTING_BLANK_HEADER
    text = (PROMPTS_DIR / "06_request.md").read_text(encoding="utf-8")
    assert EXISTING_BLANK_HEADER.removeprefix("## ") in text



# ---- 2.5: выжимка для эксперта — отдельный ключ рядом с полным текстом ----

_BRIEF_OPEN, _BRIEF_CLOSE = "<выжимка_для_эксперта>", "</выжимка_для_эксперта>"


def _brief_block(path) -> str:
    text = path.read_text(encoding="utf-8")
    assert text.count(_BRIEF_OPEN) == 1 and text.count(_BRIEF_CLOSE) == 1, path.name
    return text[text.index(_BRIEF_OPEN):text.index(_BRIEF_CLOSE)]


def test_indicator_prompts_share_one_brief_block_and_key():
    """Блок выжимки одинаков во всех 12 промптах индикаторов — правило пишется одно на всех;
    ключ rationale_brief есть в примере ответа каждого."""
    paths = [PROMPTS_DIR / f"02_indicator_{n:02d}.md" for n in range(1, 13)]
    blocks = {_brief_block(p) for p in paths}
    assert len(blocks) == 1
    block = blocks.pop()
    assert "rationale_brief" in block and "Смотрел:" in block and "Не нашёл:" in block
    for p in paths:
        assert '"rationale_brief": [' in p.read_text(encoding="utf-8"), p.name


def test_hypotheses_prompts_share_one_brief_block():
    """Блок выжимки одинаков в 04a, 04b и 05 — гипотезы, основания, эффекты пишутся по одному правилу."""
    blocks = {_brief_block(p) for p in (PROMPT_HYPOTHESES_VIOLATED, PROMPT_HYPOTHESES_NO_DATA, PROMPT_MERGE)}
    assert len(blocks) == 1
    block = blocks.pop()
    for key in ("hypothesis_brief", "grounds_brief", "how_to_calculate_brief", "expected_trend_brief"):
        assert key in block, key


def test_merge_prompt_says_how_to_brief_merged_rows():
    text = PROMPT_MERGE.read_text(encoding="utf-8")
    assert "для слитой — заново по итоговой формулировке" in text


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
    1: ["включая МФЦ, и организация, которая принимает", "<границы>"],
    2: ["<границы>"],
    3: ["Ссылка на акт без указания редакции"],
    4: ["<как_решать>", "В3. То же, но заявитель лишь ВПРАВЕ"],
    5: ["<границы>"],
    6: ["247-ФЗ", "<сам_или_разъясняет>", "<не_путай_со_сроком_действия>"],
    7: ["Одна и та же процедура — проверка, согласование, экспертиза", "{{РЕГИОНЫ_СРАВНЕНИЯ}}"],
    8: ["{{РЕГИОНЫ_СРАВНЕНИЯ}}"],
    9: ["Как оценить формулировку на однозначность"],
    10: ["доклад о виде государственного контроля (надзора) (ст. 30)"],
    11: ["Сроки исполнения обязанности — твой предмет", "Срок подачи заявки — твой предмет",
         "Сроки в разных единицах", "{{РЕГИОНЫ_СРАВНЕНИЯ}}"],
    12: ["Несоразмерное требование к участнику"],
}


@pytest.mark.parametrize("n", range(1, 13))
def test_iter4_indicator_prompt_edits(n):
    text = prompt_path(n).read_text(encoding="utf-8")
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
    """К: метки регионов — только в промптах 7, 8, 11; после подстановки меток не остаётся."""
    from app.nodes.verdicts import fill_prompt_labels
    text = path.read_text(encoding="utf-8")
    has = "{{РЕГИОНЫ_СРАВНЕНИЯ}}" in text or "{{ЧИСЛО_РЕГИОНОВ}}" in text
    assert has == (path.name in {"02_indicator_07.md", "02_indicator_08.md", "02_indicator_11.md"})
    assert "{{" not in fill_prompt_labels(text, limit=5)   # потолок подставляется всегда (узел вердиктов)


def test_iter4_hypotheses_prompts():
    """Г, Ж1, Ж2, АИ в промптах гипотез: жалобы — у органа, вопрос органу с условиями, процент по исключению."""
    for path in (PROMPT_HYPOTHESES_VIOLATED, PROMPT_HYPOTHESES_NO_DATA):
        text = " ".join(path.read_text(encoding="utf-8").split())   # без учёта переносов строк
        assert "число жалоб и судебных споров по применению акта" in text
        assert "орган их не ведёт" not in text
        assert "Вопрос органу словами ставь (source «вопрос органу»" in text
        assert "Исключение: показатель, который орган сам ведёт как официальный" in text
    v = " ".join(PROMPT_HYPOTHESES_VIOLATED.read_text(encoding="utf-8").split())
    assert "прошивка (если акт её требует)" in v and "время на дорогу и курьера" not in v
    assert "сослаться на его результат" not in v


def test_iter4_request_prompt():
    """АИ, АК, АЛ, Ж2 в промпте запроса; без упоминания того, что делает код с отбором."""
    text = " ".join(PROMPT_REQUEST.read_text(encoding="utf-8").split())
    assert "Доля не запрашивается никогда" not in text and "Процентов и долей в перечне нет" not in text
    assert "«%» — только по исключению выше" in text
    assert "«Объявленные» и «проведённые» (торги, отборы, конкурсы) по умолчанию — разные показатели" in text
    assert "Если у строки несколько пунктов, выбери тот, о котором именно этот показатель" in text
    assert "«открытый вопрос»" in text and "«вопрос органу»" in text
    assert "код увидит это и запишет" not in text


def test_iter4_dialog_prompt():
    """Е, АИ, Ж2, Ж3, В в промпте диалога; операция «нигде не ведётся» есть в схеме."""
    from app.contracts import MarkNotTracked
    text = " ".join(PROMPT_DIALOG.read_text(encoding="utf-8").split())
    assert "бизнес, граждане и город" in text and "без процентов и долей" not in text
    assert "«пометить: нигде не ведётся»" in text
    assert MarkNotTracked.model_fields["kind"].annotation.__args__[0] == "пометить: нигде не ведётся"
    assert "эксперт подсказал, где искать" in text and "«Вопросы органу»" in text



def test_iter4_request_prompt_mandatory_block():
    """АМ: агент запроса знает блок обязательных показателей; средние формы — как есть."""
    text = " ".join(PROMPT_REQUEST.read_text(encoding="utf-8").split())
    assert "<обязательные_показатели>" in text and "## ОБЯЗАТЕЛЬНЫЕ ПОКАЗАТЕЛИ (JSON)" in text
    assert "Не переделывай их в счётчики" in text
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
        assert named <= set(tools), (n, named - set(tools))   # агент не зовёт то, чего у него нет
        assert "{{" not in fill_prompt_labels(text, limit=7)
    for n in (3, 4, 7, 8, 9, 10, 11, 12):
        assert "{{ПОТОЛОК_ЗАХОДОВ}} ЗАХОДОВ ПОИСКА В ИНТЕРНЕТЕ" in prompt_path(n).read_text(encoding="utf-8"), n
    t3 = prompt_path(3).read_text(encoding="utf-8").lower()
    assert "высокий риск" not in t3 and "низкий риск" not in t3 and "garant_find_hyperlinks" in t3
    assert "<правовая_база>" not in prompt_path(6).read_text(encoding="utf-8")
