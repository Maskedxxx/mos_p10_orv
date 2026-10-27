"""
Сторожа устройства кода (27.10, план модульности `4_документы/План_модульность_кода.md`, П2 и П5).

1. Имена файлов папки прогона, которые нужны двум и более модулям, записаны строкой только в app/trace.py
   (раздел «Файлы папки прогона»); остальные модули берут их оттуда. Описания модулей и функций и имена логгеров
   (getChild) не в счёт; тексты для модели (описания инструментов чтения в app/mcp_run.py) — тоже: в них имя
   стоит внутри фразы.
2. Модули не берут друг у друга внутренние имена (с «_» в начале): общее лежит в общем модуле, а то, чем
   пользуются снаружи, названо без «_».
"""

from __future__ import annotations

import ast
from pathlib import Path

import app.trace as trace

APP = Path(__file__).resolve().parent.parent / "app"
FILES = sorted(p for p in APP.rglob("*.py") if "__pycache__" not in p.parts)

# Имена из реестра app/trace.py: файлы — целиком, папки — с косой чертой после имени
_RUN_FILES = {trace.INPUT_STEM, trace.DOCUMENT_FILE, trace.PASSPORT_FILE, trace.BUCKETS_FILE, trace.MAP_FILE,
              trace.REQUEST_FILE, trace.EXCEL_FILE, trace.META_FILE, trace.PROGRESS_FILE, trace.DIALOG_STATUS_FILE}
_RUN_DIRS = {trace.VERDICTS_DIR, trace.HYPOTHESES_DIR, trace.VERSIONS_DIR}


def _docstrings(tree: ast.AST) -> set[int]:
    """id узлов-строк, которые служат описанием модуля, класса или функции."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


def _logger_names(tree: ast.AST) -> set[int]:
    """id строк внутри вызовов getChild(...) — это имена логгеров узлов, а не файлы."""
    return {id(arg) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "getChild"
            for arg in ast.walk(node)}


def _names_run_file(text: str) -> bool:
    """Строка — имя файла прогона из реестра или путь, который с него начинается («03_verdicts/…», «…/06_map.json»)."""
    if text in _RUN_FILES or text in _RUN_DIRS:
        return True
    if any(text.startswith(d + "/") for d in _RUN_DIRS):
        return True
    return any(text.startswith(f) and text[len(f):][:1] in ("", ".", "/") or text.endswith("/" + f)
               for f in _RUN_FILES if f != trace.INPUT_STEM)


def test_run_file_names_only_in_trace():
    found = []
    for path in FILES:
        if path == APP / "trace.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstrings(tree) | _logger_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                if _names_run_file(node.value):
                    found.append(f"{path.relative_to(APP.parent)}:{node.lineno}: {node.value!r}")
    assert not found, "имя файла прогона строкой вне app/trace.py — возьмите его из trace:\n" + "\n".join(found)


def test_no_private_names_imported_between_modules():
    found = []
    for path in FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {}   # имя в модуле → модуль app, откуда оно импортировано
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
                for alias in node.names:
                    if alias.name.startswith("_") and not alias.name.startswith("__"):
                        found.append(f"{path.relative_to(APP.parent)}:{node.lineno}: from {node.module} import {alias.name}")
                    else:
                        modules[alias.asname or alias.name] = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app"):
                        modules[(alias.asname or alias.name).split(".")[-1]] = alias.name
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in modules
                    and node.attr.startswith("_") and not node.attr.startswith("__")):
                found.append(f"{path.relative_to(APP.parent)}:{node.lineno}: {node.value.id}.{node.attr}")
    assert not found, "внутреннее имя другого модуля — вынесите в общий модуль или снимите «_»:\n" + "\n".join(found)
