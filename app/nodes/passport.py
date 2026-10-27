"""
Узел [1.1] пайплайна П-10 — паспорт акта (заменил саммари, сентябрь 2026).

Паспорт — структурное описание акта, полученное одним вызовом модели через
runner.call со схемой ActPassport (--json-schema): реквизиты, статус, адресат,
природа и семь объектов (требования, процедура, документы, сроки, ссылки,
распределение, контроль) с состоянием «есть / нет / не понял» и обоснованием.
Паспорт отвечает на вопрос «где искать», а не «что написано»: агенты
индикаторов строят на его ключах ворота «не применимо», узлы гипотез — статус
акта и адресатов, узел запроса — годы бланка и адресата, Excel — шапку.

Почему провал паспорта останавливает прогон (в отличие от саммари, чей провал
не мешал): все 12 промптов v2 читают ключи паспорта; без него вердикт
«не применимо» недостоверен, а карта гипотез — пустая по ложной причине.

Таймаут отдельный (config.PASSPORT_TIMEOUT_S): на большом акте паспорт шёл
до 640 с, обрыв API с ретраем в стандартные 900 с не укладывается.
Артефакты: 02_passport.raw.json (сырой ответ, пишет runner) и 02_passport.json.
Стоимость прибавляется к meta.json — переиспользуется add_cost.
"""

from __future__ import annotations

import json
import re

from app.config import ALLOWED_TOOLS_TEXT, PASSPORT_TIMEOUT_S, PROMPT_PASSPORT
from app.contracts import ActPassport, ActPassportOutput, Developer
from app.logger import get_run_logger
from app.runner import call, garant_available
from app.trace import PASSPORT_FILE, add_cost, log_event, save_json

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _build_document(document_md: str) -> str:
    """Документ для stdin: только полный текст акта под заголовком-меткой."""
    return "## ПОЛНЫЙ ТЕКСТ АКТА\n\n" + document_md


# Строка задания, когда файла токена Гаранта нет (15.10): без неё модель пытается вызвать garant_search
GARANT_OFF_NOTE = "Гаранта в этом прогоне нет — разработчика определяй только по тексту акта."


def make_passport(
    document_md: str,
    run_id: str,
    *,
    timeout_s: int = PASSPORT_TIMEOUT_S,
    max_budget_usd: float | None = None,
) -> ActPassport | None:
    """
    Получить паспорт акта одним вызовом модели по схеме ActPassport.

    Возвращает None при провале вызова (ретраи внутри runner исчерпаны) — решение
    остановить прогон принимает оркестратор. Стоимость прибавляется в любом случае.
    """
    logger = get_run_logger(run_id).getChild("02_passport")
    instruction = PROMPT_PASSPORT.read_text(encoding="utf-8")
    # Гарант агенту паспорта — подсказка для поиска разработчика: без токена паспорт делается без него (15.10)
    garant = garant_available()
    if not garant:
        logger.warning("passport: файла токена Гаранта нет или он испорчен — паспорт без Гаранта")
        log_event(run_id, "garant_unavailable", who="агент 02_passport", reason="нет рабочего файла токена Гаранта")
        # описание поля developers велит искать и в Гаранте — модели сказано, что в этот раз его нет
        instruction += f"\n\n{GARANT_OFF_NOTE}\n"

    result = call(
        instruction,
        _build_document(document_md),
        ActPassportOutput,   # 15.10: правила для ответа модели; сохранённый паспорт читается как ActPassport
        run_id=run_id,
        trace_name="02_passport.raw.json",
        allowed_tools=ALLOWED_TOOLS_TEXT,
        timeout_s=timeout_s,
        max_budget_usd=max_budget_usd,
        # 2.7 (А): кто издаёт приказы во исполнение акта — подсказка, какой орган ведёт процедуру
        mcp_tools=["garant_search"] if garant else None,
    )
    add_cost(run_id, result.cost_usd)

    if not result.ok or result.value is None:
        logger.warning("passport: вызов модели провалился (%s)", result.error)
        return None

    passport = result.value
    save_json(run_id, PASSPORT_FILE, passport)
    logger.info(
        "passport done: status=%s scope=%s A=%s B=%s C=%s D=%s F=%s G1=%s G2=%s H=%s cost=$%.4f",
        passport.status.status, passport.addressee.scope,
        passport.requirements.state, passport.procedure.state, passport.documents.state,
        passport.deadlines.state, passport.references.state,
        passport.allocation.competitive.state, passport.allocation.appointed.state,
        passport.control.state, result.cost_usd,
    )
    return passport


def apply_user_developers(passport: ActPassport, names: list[str]) -> ActPassport:
    """
    Выбор пользователя при загрузке важнее ответа модели (2.7, А, решение владельца 12.10).
    Модель о выборе не знает — ей это не нужно; пометка «указан пользователем» — для Excel.
    """
    if not names:
        return passport
    developers = [Developer(name=n, scope="весь акт", basis="указан пользователем при загрузке") for n in names]
    requisites = passport.requisites.model_copy(update={"developers": developers})
    return passport.model_copy(update={"requisites": requisites})


def passport_json(passport: ActPassport) -> str:
    """Паспорт как читаемый JSON для stdin агентов (кириллица без escape)."""
    return json.dumps(passport.model_dump(mode="json"), ensure_ascii=False, indent=2)


def passport_block(passport: ActPassport | None) -> str:
    """
    Блок «## ПАСПОРТ АКТА» для stdin агентов узлов [2], [4а]/[4б], [6] — ровно
    тот заголовок, который описан во входных данных промптов v2. Пустая строка,
    если паспорта нет (вызов узла в тестах без паспорта).
    """
    if passport is None:
        return ""
    return "## ПАСПОРТ АКТА\n\n" f"```json\n{passport_json(passport)}\n```\n"


def passport_effective_year(passport: ActPassport | None) -> int | None:
    """
    Год вступления акта в силу для колонок бланка запроса (узел [6]).

    None — если паспорта нет, акт — проект (статистики применения не бывает)
    или дата в тексте не названа. Год берётся из строки даты как есть
    («21.08.2025», «1 января 2027 г.») — формат дат в актах не нормирован.
    """
    if passport is None or passport.status.status == "проект":
        return None
    date = passport.status.effective_date
    if not date:
        return None
    match = _YEAR_RE.search(date)
    return int(match.group()) if match else None
