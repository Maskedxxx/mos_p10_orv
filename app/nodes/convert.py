"""
Узел [1] пайплайна П-10 — конвертация акта в markdown.

Модель здесь не участвует: чистая конвертация через docling. Результат
сохраняется в трейс прогона как 01_document.md.

Принимаются три формата:
  .docx — docling читает напрямую;
  .pdf  — docling читает напрямую, распознаёт вёрстку и таблицы;
  .doc  — устаревший бинарный формат Word, docling его не понимает,
          поэтому сначала перегоняем в .docx через LibreOffice.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

# Модели docling (разметка страниц, OCR) лежат в локальном кэше huggingface.
# Без этой переменной docling при каждом старте спрашивает huggingface.co,
# актуальна ли модель, и на спарке этот запрос зависал без таймаута: DNS отдаёт
# IPv6-адреса первыми, а IPv6 не маршрутизируется (2026-09-04, тест PDF висел
# >5 мин, в офлайн-режиме — 4 с). Выставляется до импорта docling; если
# пользователь задал переменную сам — не трогаем.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from docling.document_converter import DocumentConverter  # noqa: E402

from app.config import ACCEPTED_EXTENSIONS, NEEDS_LIBREOFFICE
from app.logger import get_run_logger
from app.trace import DOCUMENT_FILE, save_text

# Сколько ждём LibreOffice на конвертации .doc в .docx
_LIBREOFFICE_TIMEOUT_S = 180


def _to_docx_via_libreoffice(path: Path, out_dir: Path) -> Path:
    """
    Перегнать устаревший .doc в .docx через LibreOffice в headless-режиме.

    Возвращает путь к получившемуся .docx. Бросает RuntimeError, если
    LibreOffice не отработал или файла на выходе не оказалось.
    """
    try:
        subprocess.run(
            ["soffice", "--headless", "--convert-to", "docx", "--outdir", str(out_dir), str(path)],
            capture_output=True, text=True, timeout=_LIBREOFFICE_TIMEOUT_S, check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("LibreOffice (soffice) не найден — .doc конвертировать нечем") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"LibreOffice не уложился в {_LIBREOFFICE_TIMEOUT_S} с на файле {path.name}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"LibreOffice не смог открыть {path.name}: {(exc.stderr or '').strip()[:300]}") from exc

    converted = out_dir / f"{path.stem}.docx"
    if not converted.exists():
        raise RuntimeError(f"LibreOffice не создал .docx для {path.name}")
    return converted


def convert_to_markdown(act_path: str | Path, run_id: str) -> str:
    """
    Конвертировать акт (.docx, .pdf или .doc) в markdown через docling
    и сохранить в трейс прогона как 01_document.md.

    Бросает FileNotFoundError, если файла нет; ValueError, если расширение
    не поддерживается либо результат конвертации пуст.
    """
    path = Path(act_path)
    if not path.exists():
        raise FileNotFoundError(f"файл акта не найден: {path}")

    suffix = path.suffix.lower()
    if suffix not in ACCEPTED_EXTENSIONS:
        formats = ", ".join(ACCEPTED_EXTENSIONS)
        raise ValueError(f"формат {suffix or '(без расширения)'} не поддерживается, нужен один из: {formats}")

    logger = get_run_logger(run_id)
    started = time.monotonic()

    # .doc докрутить до .docx во временной папке — она уберётся сама
    with tempfile.TemporaryDirectory() as tmp_dir:
        source = path
        if suffix in NEEDS_LIBREOFFICE:
            source = _to_docx_via_libreoffice(path, Path(tmp_dir))
            logger.info("convert: %s перегнан в .docx через LibreOffice", path.name)

        result = DocumentConverter().convert(source)
        markdown = result.document.export_to_markdown()

    if not markdown.strip():
        raise ValueError(f"docling вернул пустой документ для файла {path.name}")

    save_text(run_id, DOCUMENT_FILE, markdown)

    duration_s = time.monotonic() - started
    logger.info(
        "convert: file=%s format=%s chars=%d lines=%d duration=%.1fs",
        path.name, suffix, len(markdown), len(markdown.splitlines()), duration_s,
    )

    return markdown
