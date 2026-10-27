"""
Фирменный стиль «Экономика Москвы» (27.10, план 4_документы/План_бренд_Экономика_Москвы.md).

Цвета и шрифт — по брендбуку Комплекса экономической политики: красный #B91827 — главный цвет и сбой,
тёмно-серый #2E2E2E — текст, второстепенные кнопки и «идёт»; шрифт TT Moscow Economy (Regular — текст,
Medium — подзаголовки, Bold — заголовки). Здесь же общие стили страниц: обычные буквы на кнопках вместо ЗАГЛАВНЫХ,
крестик (а не галочка) у загруженного файла, чат по образцу Apple.

Файлы бренда — в var/brand/, вне git: репозиторий публичный, а шрифт — по лицензии заказчика.
  fonts/TTMoscowEconomy-{Regular,Medium,Bold}.woff2 — шрифт;
  header.svg — эмблемы Комплекса и ДЭПиР для шапки (стр. 71 брендбука, контуры вынуты без перерисовки);
  login_band.svg — полоса узора с эмблемами для входа (стр. 70);
  favicon.svg — знак Комплекса для вкладки браузера.
Нет файла — сервис работает как раньше: стандартный шрифт, без эмблем, значок вкладки 📊. Какие файлы есть, сервис
смотрит один раз при запуске (register): адрес /brand, шрифт и ссылки на эмблемы согласованы между собой, а папку,
положенную позже, сервис увидит после перезапуска.
"""
from __future__ import annotations

from pathlib import Path

from nicegui import app, ui

from app.config import BRAND_DIR

RED = "#B91827"      # фирменный красный (брендбук: RGB 185, 24, 39)
INK = "#2E2E2E"      # фирменный тёмно-серый (RGB 46, 46, 46)
LINE = "#CCCCCC"     # фирменный светло-серый — линия под шапкой
BUBBLE = "#E9E9EB"   # светло-серый пузырь ответа помощника в чате (оттенок пузыря собеседника в Apple «Сообщения»)
URL = "/brand"       # адрес, по которому сервис отдаёт var/brand/ (открыт без входа — нужен странице входа)
FONT = "TT Moscow Economy"
EMBLEMS_ALT = "Экономика Москвы · Департамент экономической политики и развития города Москвы"
_FONT_FILES = (("Regular", 400), ("Medium", 500), ("Bold", 700))

# Файлы бренда, которые сервис отдаёт (пути от var/brand/ через «/»), — заполняет register() при запуске
_available: frozenset[str] = frozenset()


def scan(directory: Path) -> frozenset[str]:
    """Файлы бренда в папке — пути от неё через «/»; папки нет — пусто."""
    if not directory.is_dir():
        return frozenset()
    return frozenset(p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file())


def url(name: str) -> str | None:
    """Адрес файла бренда для страницы; None — при запуске сервиса файла не было."""
    return f"{URL}/{name}" if name in _available else None


def favicon() -> str:
    """Значок вкладки: знак Комплекса — SVG прямо в странице (без отдельного запроса, значит виден и до входа);
    файла нет — прежний 📊."""
    if "favicon.svg" not in _available:
        return "📊"
    return (BRAND_DIR / "favicon.svg").read_text(encoding="utf-8")


def head_html() -> str:
    """Шрифт (только те начертания, чьи файлы есть) и общие стили — в <head> каждой страницы."""
    faces = "".join(
        f"@font-face {{ font-family: '{FONT}'; src: url('{link}') format('woff2'); font-weight: {weight}; "
        "font-style: normal; font-display: swap; }\n"
        for style, weight in _FONT_FILES
        if (link := url(f"fonts/TTMoscowEconomy-{style}.woff2")) is not None
    )
    return f"""<style>
{faces}html body {{ font-family: '{FONT}', Roboto, -apple-system, 'Helvetica Neue', Helvetica, Arial, sans-serif; color: {INK}; }}
/* шапка белая с тонкой линией снизу — как «типовая страница» брендбука (по умолчанию была голубая) */
.q-header.q-layout__section--marginal {{ background: #fff; color: {INK}; border-bottom: 1px solid {LINE}; }}
/* надписи на кнопках — обычными буквами, а не ЗАГЛАВНЫМИ (удобство, 27.10) */
body .q-btn {{ text-transform: none; }}
/* кнопки без заливки и с рамкой — тёмно-серые: красный остаётся у главной кнопки и у сбоя (решение 27.10).
   Цвет задаёт правило Quasar .text-primary с !important — переопределяем не цвет, а его переменную на кнопке */
body .q-btn--flat.text-primary, body .q-btn--outline.text-primary {{ --q-primary: {INK}; }}
/* поле загрузки акта — без технической строки «0.0B / 0.00%»: имя принятого файла сервис пишет сам */
.p10-upload .q-uploader__subtitle {{ display: none; }}
/* в шапке поля — только «+» (в нём поле выбора файла): «убрать загруженные» с двойной галочкой дублирует крестик
   у файла и читается как «готово» (27.10) */
.p10-upload .q-uploader__header .q-btn:not(:has(input)) {{ display: none; }}
/* чат (27.10, по образцу Apple): ответ помощника — мягкий светло-серый пузырь слева, свои сообщения — тёмно-серые
   справа; без «хвостиков», крупное скругление. Quasar красит пузырь через color (фон = currentColor) */
html body .q-message .q-message-text {{ border-radius: 18px; padding: 10px 14px; }}
html body .q-message .q-message-text:last-child:before {{ display: none; }}
html body .q-message .q-message-text--received {{ color: {BUBBLE}; }}
html body .q-message .q-message-text-content--received {{ color: {INK}; }}
html body .q-message .q-message-text--sent {{ color: {INK}; }}
html body .q-message .q-message-text-content--sent {{ color: #fff; }}
/* ссылки в тексте ответа (Markdown Гаранта) — фирменным красным, как ссылки на источники (план, п. 4.4) */
html body .q-message .q-message-text-content a {{ color: {RED}; }}
</style>
<script>
  // У загруженного файла Quasar рисует галочку, хотя нажатие на неё убирает файл, — ставим крестик (27.10).
  // Штатный способ Quasar: значок меняется в наборе значков ($q.iconSet = Quasar.IconSet.props)
  window.addEventListener("load", () => {{ if (window.Quasar) Quasar.IconSet.props.uploader.done = "clear"; }});
</script>"""


def register() -> None:
    """
    Один раз при запуске сервиса, до ui.run: какие файлы бренда есть и адрес /brand для них; фирменные цвета, шрифт
    и общие стили — на все страницы, включая служебные страницы NiceGUI (404, 500).
    """
    global _available
    _available = scan(BRAND_DIR)
    if _available:
        app.add_static_files(URL, BRAND_DIR)
    # Цвета на всё приложение: главный и «сбой» — фирменный красный; ink — тёмно-серый для «идёт» и спокойных значков
    # (классы text-ink, color="ink")
    app.colors(primary=RED, negative=RED, ink=INK)
    ui.add_head_html(head_html(), shared=True)
