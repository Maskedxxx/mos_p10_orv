"""
Страницы Гарант-помощника (платформа, шаги 3–4; владелец 23.10): список бесед и беседа с агентом.

Подключаются из app/web.py вызовом register(_layout, _who). Сам модуль app.web не импортирует: прод запускается
как `python -m app.web`, и повторный импорт исполнил бы модуль заново. Вход проверяет AuthMiddleware (app/auth.py) —
он накрывает все адреса сервиса, в том числе эти страницы и скачивание файлов (/_nicegui/auto/ закрыт без входа).

Логика беседы — app/garant_chat.py. Страница только показывает беседу, принимает документ и сообщение
и запускает ход в фоне (nicegui.run.io_bound) — страница при этом не встаёт.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable

from fastapi import Request
from nicegui import run as nicegui_run
from nicegui import ui
from nicegui.events import UploadEventArguments

from app import garant_chat, garant_limits
from app.actions import Who, log_action
from app.config import ACCEPTED_EXTENSIONS, GARANT_LIMITS_CACHE_S, GARANT_POLL_S, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB
from app.trace import log_event

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
# Строки сервиса в беседе: метка → значок и цвет (27.10: «документ» тёмно-серым — красный только у сбоя)
_MARKS = {"документ": ("description", "ink"), "лимиты": ("account_balance_wallet", "amber-8"),
          "сбой": ("error", "negative")}


def sources_html(sources: list[str]) -> str:
    """Источники ответа — HTML под ответом, адреса страниц — ссылками. Текст экранируется здесь."""
    if not sources:
        return ""
    items = []
    for src in sources:
        text = html.escape(src)
        items.append(_URL_RE.sub(lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener" '
                                           f'class="text-primary underline">{m.group(0)}</a>', text))
    return '<div class="text-xs text-gray-600 mt-2">Источники:<br>' + "<br>".join(f"• {i}" for i in items) + "</div>"


def upload_error(filename: str, size: int) -> str | None:
    """Проверка документа до чтения: формат и размер — как на странице загрузки прогона."""
    if not filename.lower().endswith(ACCEPTED_EXTENSIONS):
        return f"Файл «{filename}» не подходит: нужен {', '.join(ACCEPTED_EXTENSIONS)}."
    if size == 0:
        return f"Файл «{filename}» пустой."
    if size > MAX_UPLOAD_BYTES:
        return f"Файл «{filename}» больше {MAX_UPLOAD_MB} МБ."
    return None


def register(layout: Callable[[Who], None], who_of: Callable[[Request], Who]) -> None:
    """Объявить страницы /garant и /garant/{chat_id}; layout — общая шапка сервиса, who_of — кто открыл страницу."""

    @ui.page("/garant")
    def page_chats(request: Request) -> None:
        """Список бесед: все, новые сверху, и кнопка «Новая беседа»."""
        who = who_of(request)
        layout(who)

        def create() -> None:
            chat_id = garant_chat.new_chat(who.session)
            log_action(who, "Гарант-помощник: новая беседа", garant_chat.chat_run_id(chat_id))
            ui.navigate.to(f"/garant/{chat_id}")

        with ui.column().classes("w-full items-center p-8"):
            with ui.card().classes("w-full max-w-3xl p-6 gap-4"):
                ui.label("Гарант-помощник").classes("text-xl font-bold")
                ui.label("Приложите документ и спрашивайте: действует ли акт, какие у него редакции, на какие "
                         "акты он ссылается, есть ли судебная практика. Помощник ищет в Гаранте и в интернете, "
                         "отвечает со ссылками и по просьбе делает файл — справку, таблицу.").classes(
                    "text-sm text-gray-700")
                ui.button("Новая беседа", icon="add", on_click=create).props("unelevated")
                chats = garant_chat.list_chats()
                if not chats:
                    ui.label("Бесед пока нет.").classes("text-sm text-gray-500")
                for chat in chats:
                    with ui.row().classes("w-full items-baseline justify-between gap-4 border-t pt-2"):
                        ui.link(chat["title"], f"/garant/{chat['chat_id']}").classes("text-primary")
                        when = (chat["created_at"] or "")[:16].replace("T", " ")
                        meta = f"{when} · сообщений {chat['messages']} · документов {chat['documents']}"
                        ui.label(meta).classes("text-xs text-gray-500 whitespace-nowrap")

    @ui.page("/garant/{chat_id}")
    def page_chat(chat_id: str, request: Request) -> None:
        """Беседа: документы, переписка, поле сообщения."""
        who = who_of(request)
        layout(who)
        if not garant_chat.valid_chat_id(chat_id):
            with ui.column().classes("w-full items-center p-8"):
                with ui.card().classes("w-full max-w-2xl p-6 gap-2"):
                    ui.label("Такой беседы нет").classes("text-lg font-medium")
                    ui.link("К списку бесед", "/garant")
            return
        _ChatView(chat_id, who)


class _ChatView:
    """Экран беседы: опрашивает папку беседы раз в GARANT_POLL_S и перерисовывает то, что изменилось."""

    def __init__(self, chat_id: str, who: Who) -> None:
        self._chat_id = chat_id
        self._rid = garant_chat.chat_run_id(chat_id)
        self._who = who
        self._shown = -1
        self._shown_docs = None
        self._sending = False
        self._reading = False
        with ui.column().classes("w-full items-center p-6"):
            with ui.card().classes("w-full max-w-4xl p-6 gap-3"):
                with ui.row().classes("w-full items-baseline justify-between"):
                    self._title = ui.label("").classes("text-xl font-bold")
                    ui.link("Все беседы", "/garant").classes("text-sm text-primary")
                self._docs = ui.column().classes("w-full gap-1")
                with ui.row().classes("w-full items-center gap-3"):
                    # Сам загрузчик спрятан — файл выбирается кнопкой
                    self._upload = (
                        ui.upload(on_upload=self._on_upload, auto_upload=True, max_files=1,
                                  on_begin_upload=lambda _: self._begin_upload(),
                                  on_rejected=lambda _: self._upload_failed(),
                                  max_file_size=MAX_UPLOAD_BYTES)
                        .props(f'accept="{",".join(ACCEPTED_EXTENSIONS)}"')
                        .classes("hidden")
                    )
                    self._upload.on("failed", lambda: self._upload_failed())
                    self._upload_button = ui.button("Приложить документ", icon="upload_file",
                                                    on_click=lambda: self._upload.run_method("pickFiles")).props(
                        "outline")
                    self._limits = ui.label("").classes("text-xs text-gray-500")
                with ui.scroll_area().classes("w-full h-[55vh] border rounded") as self._scroll:
                    self._chat = ui.column().classes("w-full gap-2 p-2")
                self._status = ui.label("").classes("text-sm text-ink")   # 27.10: «идёт» тёмно-серым
                with ui.row().classes("w-full items-end flex-nowrap gap-2"):
                    self._input = ui.textarea(placeholder="Сообщение помощнику (Ctrl+Enter — отправить)").props(
                        "autogrow outlined").classes("flex-grow")
                    self._send = ui.button(icon="send", on_click=self._on_send).props("unelevated")
        self._input.on("keydown.ctrl.enter", self._on_send)
        ui.timer(GARANT_POLL_S, self._tick)
        ui.timer(GARANT_LIMITS_CACHE_S, self._refresh_limits)
        ui.timer(0.1, self._refresh_limits, once=True)
        self._tick()

    # ---- опрос ----

    def _tick(self) -> None:
        info = garant_chat.chat_info(self._chat_id)
        self._title.set_text(info.title or "Новая беседа")
        docs = info.documents
        chat = garant_chat.read_chat(self._chat_id)
        if len(chat) != self._shown:
            self._shown = len(chat)
            self._render_chat(chat)
        state = garant_chat.busy_state(self._chat_id)
        if self._reading or state == "читает документ":
            self._status.set_text("Читаю документ…")
        elif state == "удаляет документ":
            self._status.set_text("Удаляю документ…")
        elif state == "в очереди":
            self._status.set_text("Помощник ждёт очереди: сейчас отвечают в других беседах…")
        elif state == "ищет" or self._sending:
            self._status.set_text("Помощник ищет… обычно до минуты, с поиском в Гаранте — дольше")
        else:
            self._status.set_text("")
        free = state is None and not self._sending and not self._reading
        docs_key = (tuple((d.n, d.name) for d in docs), free)
        if docs_key != self._shown_docs:
            self._shown_docs = docs_key
            self._docs.clear()
            with self._docs:
                if not docs:
                    ui.label("Документ не приложен — можно спрашивать и без него.").classes("text-sm text-gray-700")
                for document in docs:
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(f"Документ «{document.name}»").classes("text-sm text-gray-700")
                        button = ui.button("Удалить", icon="delete_outline",
                                           on_click=lambda d=document: self._confirm_remove(d)).props("flat dense")
                        button.set_enabled(free)
        for element in (self._send, self._upload_button):
            element.set_enabled(free)

    async def _refresh_limits(self) -> None:
        left = (await nicegui_run.io_bound(garant_limits.limits) or {}).get(garant_limits.EXPORT)
        self._limits.set_text(f"Выгрузок полного текста из Гаранта осталось в этом месяце: {left} (на всех)"
                              if left is not None else "Остаток выгрузок Гаранта узнать не удалось")

    def _begin_upload(self) -> None:
        self._reading = True
        self._tick()

    def _upload_failed(self) -> None:
        self._reading = False
        self._tick()
        ui.notify("Не удалось загрузить документ. Проверьте формат и размер файла.", type="negative")

    def _render_chat(self, chat: list[dict]) -> None:
        self._chat.clear()
        with self._chat:
            if not chat:
                ui.label("Здесь будет переписка с помощником.").classes("text-sm text-gray-500 p-2")
            for message in chat:
                if message.role == "сервис":
                    icon, color = _MARKS.get(message.mark, ("info", "grey-6"))
                    with ui.row().classes("w-full items-start flex-nowrap gap-2 px-3 py-2 bg-gray-100 rounded"):
                        ui.icon(icon, color=color).classes("text-lg")
                        ui.label(message.text).classes("text-sm text-gray-800 whitespace-pre-line")
                    continue
                stamp = message.at[11:16]
                if message.role == "пользователь":
                    ui.chat_message(text=message.text, name="Вы", sent=True, stamp=stamp).classes("w-full")
                    continue
                with ui.chat_message(name="Помощник", sent=False, stamp=stamp).classes("w-full"):
                    ui.markdown(message.text)
                    if message.sources:
                        ui.html(sources_html(message.sources), sanitize=False)
                    for f in message.files or []:
                        ui.button(f"Скачать «{f.name}»", icon="download",
                                  on_click=lambda name=f.name: self._download(name)).props("outline dense")
                if message.steps:   # что помощник делал в этом ходе — свёрнуто под ответом
                    with ui.expansion(f"Шаги помощника ({len(message.steps)})").classes(
                            "w-full text-xs text-gray-600"):
                        for step in message.steps:
                            ui.label(f"→ {step}").classes("text-xs text-gray-600")
        self._scroll.scroll_to(percent=1.0)

    # ---- действия ----

    async def _on_send(self) -> None:
        text = (self._input.value or "").strip()
        if not text:
            ui.notify("Напишите сообщение", type="warning")
            return
        if self._sending or self._reading or garant_chat.busy_state(self._chat_id) is not None:
            ui.notify("Помощник ещё отвечает — дождитесь ответа", type="warning")
            return
        self._sending = True
        self._input.set_value("")
        self._tick()
        log_action(self._who, "Гарант-помощник: сообщение", self._rid, chars=len(text))
        try:
            await nicegui_run.io_bound(garant_chat.turn, self._chat_id, text, session=self._who.session)
        except Exception as error:   # сбой вне вызова модели (диск) — в беседу понятной строкой, техника — в журнал
            log_event(self._rid, "garant_turn_crashed", error=str(error)[:300])
            garant_chat.append_chat(self._chat_id, "сервис", "Не получилось ответить: сбой сервиса. "
                                    "Попробуйте ещё раз.", mark="сбой", session=self._who.session)
        finally:
            self._sending = False

    async def _on_upload(self, event: UploadEventArguments) -> None:
        name = event.file.name
        error = upload_error(name, event.file.size())
        if error is not None:
            ui.notify(error, type="negative")
            self._upload.reset()
            self._reading = False
            self._tick()
            return
        data = await event.file.read()
        self._reading = True
        self._tick()
        log_action(self._who, "Гарант-помощник: документ", self._rid, file=name, size=len(data))
        try:
            chars = await nicegui_run.io_bound(garant_chat.add_document, self._chat_id, name, data,
                                               self._who.session)
            ui.notify(f"Документ «{name}» приложен: {chars:,} знаков".replace(",", " "), type="positive")
        except ValueError as failure:
            ui.notify(str(failure), type="negative", multi_line=True)
        finally:
            self._reading = False
            self._upload.reset()

    def _confirm_remove(self, document) -> None:
        if self._reading or self._sending or garant_chat.busy_state(self._chat_id) is not None:
            ui.notify("Дождитесь завершения текущего действия.", type="warning")
            return
        with ui.dialog() as box, ui.card():
            ui.label(f"Удалить документ «{document.name}»?").classes("text-lg font-medium")
            ui.label("Документ и его текст будут убраны из вложений. История сообщений останется и может содержать цитаты.")\
                .classes("text-sm text-gray-700")
            with ui.row().classes("justify-end gap-2"):
                ui.button("Отмена", on_click=box.close).props("flat")
                ui.button("Удалить", on_click=lambda: self._remove_document(document.n, box)).props("unelevated")
        box.open()

    def _remove_document(self, number: int, box: ui.dialog) -> None:
        box.close()
        if self._reading or self._sending:
            ui.notify("Дождитесь завершения текущего действия.", type="warning")
            return
        try:
            garant_chat.remove_document(self._chat_id, number, self._who.session)
        except ValueError as error:
            ui.notify(str(error), type="warning")
            return
        log_action(self._who, "Гарант-помощник: удалён документ", self._rid, number=number)
        self._tick()

    def _download(self, name: str) -> None:
        path = garant_chat.file_path(self._chat_id, name)
        if path is None:
            ui.notify("Файл не найден — попросите помощника сделать его ещё раз", type="warning")
            return
        log_action(self._who, "Гарант-помощник: скачивание файла", self._rid, file=name)
        ui.download.file(path, name)
