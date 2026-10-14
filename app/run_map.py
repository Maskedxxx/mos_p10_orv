"""
Карта прогона для помощника (итерация 4, 14.10, план `4_документы/План_помощника_итерации_4.md`, шаг 1).

Помощник после прогона отвечает по артефактам прогона. Чтобы он сам понимал, где что лежит, ему в системный промпт
уходит эта карта:
  1) какие файлы есть в папке прогона, что в каждом и по какому контракту он записан;
  2) ВСЕ поля каждого контракта — полностью, как в Field(description=…), вложенные модели — рекурсивно;
  3) таблица «лист и колонка Excel ← файл и поле».

Описание полей собирается кодом из тех же pydantic-контрактов, по которым работают агенты, — поменяли контракт,
карта обновилась сама. Руками ведутся только список файлов (ARTIFACTS) и таблица Excel (EXCEL_MAP); тест проверяет,
что каждое поле из таблицы Excel существует в контракте.

Вход: ничего (контракты из app/contracts.py, контракты индикаторов из app/nodes/verdicts.INDICATOR_OUTPUT).
Выход: build_run_map() -> str (markdown).
"""

from __future__ import annotations

import typing
from dataclasses import dataclass
from functools import lru_cache

from pydantic import BaseModel

from app.contracts import (
    ActPassport, Buckets, DataRequest, HypothesisMap, HypothesisSet, JournalEntry, RunMeta, Verdict, VersionState,
)


@dataclass(frozen=True)
class Artifact:
    """Один файл папки прогона: путь (NN — номер индикатора 01–12), контракт (None — не JSON), что в нём."""

    path: str
    contract: type[BaseModel] | None
    what: str


# Файлы прогона в порядке графа пайплайна; «корень папки прогона — текущая версия» (app/versions.py)
ARTIFACTS: list[Artifact] = [
    Artifact("01_document.md", None, "Полный текст проверяемого акта (markdown), из загруженного файла."),
    Artifact("02_passport.json", ActPassport, "Паспорт акта: структурное описание — где что в акте искать."),
    Artifact("03_verdicts/NN.json", Verdict, "Вердикт индикатора NN: вердикт, разбор, цитата, пункт, источники."),
    Artifact("03_verdicts/NN.checks.json", None,
             "Таблица проверки индикатора NN — на ней агент поставил вердикт (поля — ниже, по индикаторам). "
             "Это исходная проверка агента; ручные правки вердикта — в versions/journal.jsonl."),
    Artifact("04_buckets.json", Buckets, "Вердикты, разложенные на кучи: нарушен / нет данных / проверено / ошибка."),
    Artifact("05_hypotheses/NN.json", HypothesisSet, "Гипотезы по индикатору NN (для «нарушен» и «нет данных» 7–12)."),
    Artifact("06_map.json", HypothesisMap, "Карта гипотез: строки — проблемы акта, слитые из гипотез индикаторов."),
    Artifact("07_request.json", DataRequest, "Бланк запроса показателей органу: позиции, годы, что не вошло."),
    Artifact("08_result.xlsx", None, "Excel для эксперта — собирается КОДОМ из файлов выше (таблица ниже)."),
    Artifact("meta.json", RunMeta, "Сведения о прогоне: имя файла, тип акта, настройки, стоимость."),
    Artifact("versions/state.json", VersionState, "Состояние версий диалога: текущая версия, пометки строк."),
    Artifact("versions/journal.jsonl", JournalEntry, "Журнал изменений после прогона: по строке JSON на изменение."),
]


@dataclass(frozen=True)
class ExcelColumn:
    """Колонка листа Excel и откуда она берётся: файл и путь поля (rows[].proof — поле proof каждой строки)."""

    sheet: str
    column: str
    file: str
    fields: tuple[str, ...]
    note: str = ""


# Руками — только это соответствие (app/nodes/excel.py); тест проверяет, что все поля существуют в контрактах
EXCEL_MAP: list[ExcelColumn] = [
    ExcelColumn("Карта гипотез", "№", "06_map.json", ("rows[].row_id",)),
    ExcelColumn("Карта гипотез", "Индикаторы", "06_map.json", ("rows[].indicators",)),
    ExcelColumn("Карта гипотез", "Гипотеза", "06_map.json", ("rows[].hypothesis_brief", "rows[].hypothesis"),
                "выжимка; если её нет — полный текст"),
    ExcelColumn("Карта гипотез", "Эффект", "06_map.json", ("rows[].effects",)),
    ExcelColumn("Карта гипотез", "Данные", "06_map.json", ("rows[].data",),
                "с номерами позиций бланка из 07_request.json items[].for_rows"),
    ExcelColumn("Карта гипотез", "На чём основано", "06_map.json", ("rows[].proof", "rows[].clause", "rows[].grounds_brief"),
                "цитата строки (нет — цитата вердикта), пункт, источники вердиктов этих индикаторов"),
    ExcelColumn("Запрос показателей", "№ / Показатель", "07_request.json", ("items[].num", "items[].metric", "items[].unit"),
                "блоки: обязательные показатели (items[].mandatory), из гипотез, перечни; подстроки N.1, N.2 — items[].breakdown"),
    ExcelColumn("Запрос показателей", "Откуда запрос", "07_request.json", ("items[].for_rows", "items[].mandatory")),
    ExcelColumn("Запрос показателей", "Пункт акта", "07_request.json", ("items[].clause",)),
    ExcelColumn("Запрос показателей", "годы", "07_request.json", ("years",), "колонки лет; клетки заполняет орган"),
    ExcelColumn("Вопросы органу", "№ / Вопрос / Пункт / Строки карты", "07_request.json",
                ("items[].num", "items[].metric", "items[].clause", "items[].for_rows"),
                "позиции с items[].kind = «открытый вопрос»"),
    ExcelColumn("Вердикты", "Индикатор / Вердикт", "03_verdicts/NN.json", ("indicator", "name", "verdict")),
    ExcelColumn("Вердикты", "Обоснование", "03_verdicts/NN.json", ("rationale_brief", "rationale"),
                "выжимка; если её нет — полный разбор"),
    ExcelColumn("Вердикты", "На чём основано", "03_verdicts/NN.json", ("proof", "clause", "sources")),
    ExcelColumn("Вердикты", "Где подробнее", "06_map.json", ("rows[].row_id", "rows[].indicators"),
                "строки карты этого индикатора"),
    ExcelColumn("Ошибки прогона", "Индикатор / Что случилось / Попыток", "04_buckets.json",
                ("failed[].indicator", "failed[].rationale", "failed[].attempts"),
                "плюс «нарушен», по которому нет строки карты (гипотезы не построены)"),
    ExcelColumn("Журнал изменений", "все колонки", "versions/journal.jsonl",
                ("version", "at", "action", "target", "before", "after", "reason", "kind", "cost_usd")),
    ExcelColumn("Как заполнять", "реквизиты и тип акта", "02_passport.json", ("requisites",),
                "тип акта — из meta.json act_types"),
]


def _type_label(annotation: object) -> str:
    """Короткая подпись типа поля: «текст», «число», «список из …», значения Literal перечислением."""
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin is typing.Literal:
        return "одно из: " + ", ".join(f"«{a}»" for a in args)
    if origin in (list, tuple, set):
        return "список: " + (_type_label(args[0]) if args else "значения")
    if origin is dict:
        return "словарь"
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        parts = [_type_label(a) for a in args if a is not type(None)]
        return " или ".join(parts) + (" или null" if type(None) in args else "")
    if origin is typing.Annotated:
        return _type_label(args[0])
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return f"объект {annotation.__name__}"
    return {str: "текст", int: "целое число", float: "число", bool: "да/нет"}.get(annotation, str(annotation))


def _nested_models(annotation: object) -> list[type[BaseModel]]:
    """Модели, вложенные в тип поля (list[X], X | None, Union[…]) — для рекурсивного описания."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    found: list[type[BaseModel]] = []
    for arg in typing.get_args(annotation):
        found += _nested_models(arg)
    return found


def describe_model(model: type[BaseModel], indent: int = 0, seen: set[type] | None = None,
                   only: list[str] | None = None) -> list[str]:
    """
    Строки описания модели: по полю — имя, тип, описание полностью; вложенные модели — рекурсивно, каждая один раз.
    only — описывать только эти поля (таблицы индикаторов: поля сверх Verdict).
    """
    seen = set() if seen is None else seen
    pad = "  " * indent
    lines: list[str] = []
    for name, info in model.model_fields.items():
        if only is not None and name not in only:
            continue
        desc = " ".join((info.description or "").split())
        lines.append(f"{pad}- `{name}` ({_type_label(info.annotation)}): {desc}".rstrip(": "))
        for sub in _nested_models(info.annotation):
            if sub in seen:
                continue
            seen.add(sub)
            doc = " ".join((sub.__doc__ or "").split())
            lines.append(f"{pad}  `{sub.__name__}` — {doc}")
            lines += describe_model(sub, indent + 2, seen)
    return lines


def _checks_section() -> list[str]:
    """Поля таблиц проверки по индикаторам: то, что в контракте индикатора сверх вердикта (03_verdicts/NN.checks.json)."""
    from app.config import get_indicator
    from app.nodes.verdicts import INDICATOR_OUTPUT   # здесь, а не наверху: узел вердиктов тянет runner и конфиг

    lines = ["", "## Таблицы проверки индикаторов — `03_verdicts/NN.checks.json`", ""]
    for n in sorted(INDICATOR_OUTPUT):
        contract = INDICATOR_OUTPUT[n]
        extra = [f for f in contract.model_fields if f not in Verdict.model_fields]
        if not extra:
            continue
        lines.append(f"### Индикатор {n} «{get_indicator(n).name}» — `{n:02d}.checks.json`")
        lines += describe_model(contract, only=extra)
        lines.append("")
    return lines


@lru_cache(maxsize=1)
def build_run_map() -> str:
    """Карта прогона целиком (markdown). Кэшируется: контракты за жизнь процесса не меняются."""
    lines = [
        "# КАРТА ПРОГОНА",
        "",
        "Папка прогона — текущая версия результата; снимки версий — в versions/vN/. Excel собирает код из JSON-файлов,",
        "сам файл Excel не правится: правка — операцией из плана, код меняет JSON и пересобирает Excel.",
        "",
        "## Файлы",
        "",
    ]
    for art in ARTIFACTS:
        lines.append(f"- `{art.path}` — {art.what}" + (f" Контракт `{art.contract.__name__}`." if art.contract else ""))
    lines += ["", "## Excel: лист и колонка ← файл и поле", ""]
    for col in EXCEL_MAP:
        note = f" — {col.note}" if col.note else ""
        lines.append(f"- «{col.sheet}», {col.column} ← `{col.file}`: {', '.join(col.fields)}{note}")
    for art in ARTIFACTS:
        if art.contract is None:
            continue
        lines += ["", f"## `{art.path}` — `{art.contract.__name__}`", ""]
        doc = " ".join((art.contract.__doc__ or "").split())
        if doc:
            lines.append(doc)
        lines += describe_model(art.contract)
    lines += _checks_section()
    return "\n".join(lines).rstrip() + "\n"


def resolve_field(contract: type[BaseModel], path: str) -> bool:
    """Существует ли поле по пути «rows[].proof» в контракте (для теста таблицы Excel)."""
    model: type[BaseModel] | None = contract
    for part in path.replace("[]", "").split("."):
        if model is None or part not in model.model_fields:
            return False
        nested = _nested_models(model.model_fields[part].annotation)
        model = nested[0] if nested else None
    return True
