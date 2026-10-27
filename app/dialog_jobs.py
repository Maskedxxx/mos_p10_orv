"""
Долгие действия чата по прогону и замок правки (27.10, модульность: вынесены из app/web.py без изменений).

    DialogLock    правит одно окно, остальные окна того же прогона — только просмотр
    DialogJobs    ход помощника, применение плана, возврат версии — живут на сервере, а не в окне браузера;
                  что идёт сейчас — dialog/status.json, результат агента ждёт «Принять» / «Отклонить» здесь
    тексты чата   шаг плана словами, итог шага и плана, что устарело
    конец сессии  кнопка «Завершить работу» и закрытие по простою (таймер NiceGUI раз в SESSION_IDLE_CHECK_S)

Ход помощника — app/dialog.py, выполнение плана — app/operations.py, версии — app/versions.py. Страница
(app/web.py) только показывает состояние и передаёт сюда нажатия.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import datetime

from nicegui import app as nicegui_app
from nicegui import run as nicegui_run
from pydantic import TypeAdapter

from app import dialog, operations, sessions, versions
from app.actions import Who, log_action
from app.config import DIALOG_LOCK_TTL_S, SESSION_IDLE_CHECK_S
from app.contracts import ChatEntry, Marks, Operation
from app.formatting import plural, shorten
from app.logger import app_logger
from app.trace import read_progress

_OPERATION = TypeAdapter(Operation)


class DialogLock:
    """
    Замок правки прогона: правит одно окно, остальные окна того же прогона —
    только просмотр. Окно-хозяин продлевает замок на каждом опросе; время
    передаётся параметром, как в LoginThrottle, — чтобы тест не спал.
    """

    def __init__(self, ttl_s: float = DIALOG_LOCK_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._held: dict[str, tuple[str, float]] = {}   # прогон → (окно, когда продлён)

    def acquire(self, run_id: str, client_id: str, now: float) -> bool:
        """Взять или продлить замок; False — прогон правит другое окно."""
        holder = self._held.get(run_id)
        if holder is None or holder[0] == client_id or now - holder[1] > self._ttl_s:
            self._held[run_id] = (client_id, now)
            return True
        return False

    def release(self, run_id: str, client_id: str) -> None:
        """Снять замок при закрытии окна — только свой."""
        if self._held.get(run_id, ("", 0.0))[0] == client_id:
            del self._held[run_id]


DIALOG_LOCK = DialogLock()


def op_title(op: Operation) -> str:
    """Шаг плана человеческими словами — для карточки плана."""
    kind = op.kind
    if kind == "правка текста":
        where = {"вердикт": f"Вердикты, индикатор {op.key}", "строка карты": f"строка карты {op.key}",
                 "позиция запроса": f"позиция запроса {op.key}"}[op.sheet]
        effect = f", эффект {op.effect_index}" if op.effect_index else ""
        return f"Поправить текст — {where}, поле «{op.field}»{effect}: «{shorten(op.new_text, 400)}»"
    if kind == "смена вердикта":
        return f"Сменить вердикт индикатора {op.indicator} на «{op.verdict}»: {shorten(op.rationale, 300)}"
    if kind == "удалить строку карты":
        return f"Удалить строку карты {op.row_id}"
    if kind == "добавить строку карты":
        indicators = ", ".join(map(str, op.indicators))
        return f"Добавить строку карты (индикаторы {indicators}): «{shorten(op.hypothesis, 300)}»"
    if kind == "слить строки карты":
        rows = ", ".join(map(str, op.row_ids))
        return f"Слить строки карты {rows} в одну: «{shorten(op.hypothesis, 300)}»"
    if kind == "пометить: нигде не ведётся":
        return f"Пометить: нигде не ведётся — строка карты {op.row_id}, цифра «{shorten(op.metric, 200)}»"
    if kind == "удалить позицию запроса":
        return f"Удалить позицию запроса {op.num}"
    if kind == "разделить позицию запроса":
        parts = "; ".join(f"«{part.metric}», {part.unit}" for part in op.parts)
        return f"Разделить позицию запроса {op.num} на {len(op.parts)}: {parts}"
    if kind == "перепроверить индикатор":
        return f"Перепроверить индикатор {op.indicator} его агентом. Уточнение задачи: {shorten(op.clarification, 300)}"
    if kind == "построить гипотезы":
        return f"Построить гипотезы по индикатору {op.indicator} агентом гипотез"
    return f"Дополнить запрос под строки карты {', '.join(map(str, op.row_ids))} агентом запроса"


def pending_plan(chat: list[ChatEntry]) -> list[Operation]:
    """План последнего ответа помощника, если после него ничего не было, — он ждёт решения."""
    if not chat or chat[-1].role != "ассистент":
        return []
    return [_OPERATION.validate_python(p) for p in chat[-1].plan or []]


def stale_notes(before: Marks, after: Marks) -> list[str]:
    """Что шаг пометил «устарело» — только новые пометки, по листам."""
    notes = []
    for label, old, new in (("Вердикты, инд.", before.stale_verdicts, after.stale_verdicts),
                            ("строка карты", before.stale_rows, after.stale_rows),
                            ("позиция запроса", before.stale_items, after.stale_items)):
        notes += [f"{label} {key}: {why}" for key, why in new.items() if old.get(key) != why]
    return notes


def step_text(result: operations.StepResult) -> str:
    """Итог шага для чата: версия, что изменилось, «было — стало»."""
    change = result.change
    head = f"Версия {result.version}. {change.target} — {change.action}"
    if change.before is None and change.after is None:
        return head
    before, after = change.before or "—", change.after or "—"
    if len(before) + len(after) <= 80:
        return f"{head}: «{before}» → «{after}»"
    return f"{head}\nБыло: {shorten(before, 800)}\nСтало: {shorten(after, 1500)}"


def plan_summary(total: int, applied: int, rejected: int, failed: int | None, why: str | None,
                 version: int) -> tuple[str, str]:
    """Итоговая строка плана в чат и её отметка (готово / частично / сбой / отменено)."""
    steps = f"{applied} из {total} {plural(total, 'шага', 'шагов', 'шагов')}"
    if failed is not None:
        if applied:
            return f"План применён частично: {steps}, файл — версия {version}. Шаг {failed} не выполнен — {why}.", "частично"
        return f"План не применён: шаг {failed} не выполнен — {why}. Файл не изменён (версия {version}).", "сбой"
    if applied == 0:
        return f"План завершён без изменений — файл остался в версии {version}.", "отменено"
    tail = f", отклонено {rejected}" if rejected else ""
    return f"Готово: план применён ({steps}{tail}). Файл — версия {version}, скачать можно кнопкой вверху.", "готово"


def current_version(run_id: str) -> int:
    """Текущая версия файла прогона; 0 — исходный разбор."""
    state = versions.load_state(run_id)
    return state.current if state is not None else 0


class DialogJobs:
    """
    Долгие действия диалога — ход помощника, план, возврат версии — живут на сервере,
    а не в окне браузера. Что идёт сейчас, пишется в dialog/status.json: перезагрузка
    страницы и второе окно видят то же самое. Результат агента («было — стало») ждёт
    решения здесь, пока его не примут или не отклонят, — закрытое окно его не теряет.
    По одному прогону одновременно идёт одно действие.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._waiting: dict[str, tuple[str, operations.Candidate, asyncio.Future]] = {}

    def busy(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    def waiting(self, run_id: str) -> tuple[str, operations.Candidate] | None:
        """Результат агента, который ждёт «Принять» / «Отклонить»: заголовок шага и кандидат."""
        item = self._waiting.get(run_id)
        return None if item is None else (item[0], item[1])

    def decide(self, run_id: str, accepted: bool) -> None:
        item = self._waiting.get(run_id)
        if item is not None and not item[2].done():
            item[2].set_result(accepted)

    def start(self, run_id: str, job: Coroutine) -> bool:
        """Запустить действие, если по прогону ничего не идёт; False — занято. Закрытая сессия открывается заново (23.10)."""
        if self.busy(run_id):
            job.close()
            return False
        sessions.reopen_session(run_id)
        self._tasks[run_id] = asyncio.get_running_loop().create_task(job)
        return True

    def cancel_plan(self, run_id: str, session: str | None = None) -> None:
        dialog.append_chat(run_id, "сервис", f"План отменён — файл не изменён (версия {current_version(run_id)}).",
                           mark="отменено", session=session)

    async def turn(self, run_id: str, text: str, session: str | None = None) -> None:
        """Ход помощника: ответ и, может быть, план — всё пишется в чат; session — чья реплика."""
        if pending_plan(dialog.read_chat(run_id)):
            dialog.append_chat(run_id, "сервис", "План выше не применён.", mark="отменено", session=session)
        dialog.set_status(run_id, "думает", "Помощник думает…", "обычно 10–40 сек")
        try:
            await nicegui_run.io_bound(lambda: dialog.turn(run_id, text, session=session))
        except Exception as error:   # сбой вне вызова модели (диск, артефакты) — в чат, сервис живёт
            dialog.append_chat(run_id, "сервис", f"Сбой хода помощника: {error}", mark="сбой", session=session)
        finally:
            dialog.clear_status(run_id)

    async def apply_plan(self, run_id: str, plan: list[Operation], session: str | None = None) -> None:
        """Шаги плана по одному; итог каждого и всего плана — в чат с меткой сессии; сбой останавливает остальные."""
        total = len(plan)
        applied = rejected = 0
        failed: int | None = None
        why: str | None = None
        dialog.append_chat(run_id, "сервис", f"Применяю план: {total} {plural(total, 'шаг', 'шага', 'шагов')}.",
                           session=session)
        try:
            for number, op in enumerate(plan, 1):
                title = f"Шаг {number} из {total}: {op_title(op)}"
                slow = operations.is_model_op(op)
                dialog.set_status(run_id, "шаг", shorten(f"Выполняю шаг {number} из {total}: {op_title(op)}", 160),
                                  "агент работает, обычно 1–5 мин" if slow else "обычно несколько секунд")
                before = versions.load_state(run_id)
                try:
                    result = await nicegui_run.io_bound(operations.run_step, run_id, op)
                    if result.candidate is not None:
                        if not await self._decision(run_id, title, result.candidate):
                            rejected += 1
                            dialog.append_chat(run_id, "сервис", f"Шаг {number} отклонён — файл не изменён.",
                                               mark="отменено", cost_usd=round(result.cost_usd, 4), session=session)
                            continue
                        dialog.set_status(run_id, "шаг", f"Записываю шаг {number} из {total}", "обычно несколько секунд")
                        result = await nicegui_run.io_bound(operations.accept, run_id, result.candidate)
                except Exception as error:   # OperationError — понятная причина; остальное — сбой, корень уже откатан
                    failed, why = number, (str(error) if isinstance(error, operations.OperationError) else f"сбой: {error}")
                    break
                applied += 1
                after = versions.load_state(run_id)
                notes = stale_notes(before.marks if before is not None else Marks(),
                                    after.marks if after is not None else Marks())
                text = step_text(result) + ("\nУстарело: " + "; ".join(notes) if notes else "")
                dialog.append_chat(run_id, "сервис", text, mark="шаг", version=result.version,
                                   cost_usd=round(result.cost_usd, 4), session=session)
        finally:
            dialog.clear_status(run_id)
        text, mark = plan_summary(total, applied, rejected, failed, why, current_version(run_id))
        dialog.append_chat(run_id, "сервис", text, mark=mark, session=session)

    async def _decision(self, run_id: str, title: str, candidate: operations.Candidate) -> bool:
        """Ждать «Принять» / «Отклонить» по результату агента."""
        dialog.set_status(run_id, "решение", "Ждёт вашего решения",
                          "проверьте «было — стало» ниже и нажмите «Принять» или «Отклонить»")
        future = asyncio.get_running_loop().create_future()
        self._waiting[run_id] = (title, candidate, future)
        try:
            return await future
        finally:
            self._waiting.pop(run_id, None)

    async def rollback(self, run_id: str, n: int, session: str | None = None) -> None:
        """Вернуть версию n — новой версией, ничего не стирая."""
        dialog.set_status(run_id, "шаг", f"Возвращаю версию {n}", "обычно несколько секунд")
        try:
            result = await nicegui_run.io_bound(operations.rollback, run_id, n)
            dialog.append_chat(run_id, "сервис", f"Готово: {step_text(result)}", mark="готово", version=result.version,
                               session=session)
        except Exception as error:
            dialog.append_chat(run_id, "сервис", f"Вернуть версию {n} не получилось: {error}", mark="сбой",
                               session=session)
        finally:
            dialog.clear_status(run_id)


DIALOG_JOBS = DialogJobs()


# ---- Конец сессии (2.7, мониторинг): кнопка и закрытие по простою ----

def close_by_user(who: Who, run_id: str, jobs: DialogJobs = DIALOG_JOBS) -> str | None:
    """Кнопка «Завершить работу»: None — сессия закрыта; иначе — почему сейчас нельзя."""
    if sessions.is_closed(run_id):
        return "Сессия уже закрыта."
    progress = read_progress(run_id)
    if progress is None or progress.status not in ("готов", "сбой"):
        return "Разбор ещё идёт — завершить работу можно после него."
    if jobs.busy(run_id):
        return "Идёт действие в чате — дождитесь его конца."
    log_action(who, "завершить работу", run_id)
    sessions.close_session(run_id, by="пользователь")
    return None


def close_idle_sessions(now: datetime, jobs: DialogJobs = DIALOG_JOBS) -> list[str]:
    """Закрыть сессии без событий дольше SESSION_IDLE_S; по которым идёт действие диалога — не трогать."""
    closed = []
    for rid in sessions.idle_sessions(now):
        if jobs.busy(rid):
            continue
        if sessions.close_session(rid, by="таймер", now=now):
            app_logger.info("сессия %s закрыта по простою", rid)
            closed.append(rid)
    return closed


def _check_idle() -> None:
    """Проверка простоя сессий; сбой проверки пишется в лог сервиса и не останавливает её."""
    try:
        close_idle_sessions(datetime.now())
    except Exception:
        app_logger.exception("проверка простоя сессий не удалась")


# Раз в SESSION_IDLE_CHECK_S — таймер NiceGUI, не привязанный к окну (27.10 вместо своего фонового цикла):
# NiceGUI запускает его со стартом сервиса; первая проверка, как и раньше, — через интервал после старта
nicegui_app.timer(SESSION_IDLE_CHECK_S, _check_idle, immediate=False)
