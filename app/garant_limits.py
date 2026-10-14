"""
Остаток лимитов Гаранта (итерация 4, 14.10, план помощника, шаг 5).

Лимиты API «Гарант Коннект» общие на весь токен и на месяц: выгрузки текста — 30, карточки — 300, ссылки — 1000.
Помощник и страница показывают, сколько осталось, — особенно выгрузок: их тратят все прогоны.

Вход: токен машины (~/.config/garant/token, тот же, что у агентов). Выход: limits() -> {семейство: остаток} или None,
если узнать не удалось (страница пишет «не удалось», работа не останавливается). Вызов GET /v2/limits лимит не
тратит; ответ кэшируется на CACHE_S секунд. Гарант — напрямую, мимо прокси машины (как у агентов, config.py).
Токен никуда не пишется.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from app.config import GARANT_TOKEN_FILE

LIMITS_URL = "https://api.garant.ru/v2/limits"
CACHE_S = 60
EXPORT = "Экспорт"            # семейство выгрузок текста (30 в месяц) — так оно называется в ответе /v2/limits

_cache: tuple[float, dict[str, int]] | None = None
# Без прокси машины: Гарант напрямую (через прокси вызовы зависали — см. GARANT_NO_PROXY_HOST в config.py)
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def limits(fresh: bool = False) -> dict[str, int] | None:
    """Остаток по семействам методов: {"Экспорт": 21, …}. fresh — мимо кэша (до и после хода с выгрузкой)."""
    global _cache
    if not fresh and _cache is not None and time.monotonic() - _cache[0] < CACHE_S:
        return dict(_cache[1])
    try:
        token = GARANT_TOKEN_FILE.read_text(encoding="utf-8").strip()
        req = urllib.request.Request(LIMITS_URL, headers={"Accept": "application/json",
                                                          "Authorization": f"Bearer {token}"})
        with _opener.open(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = {str(fam["title"]): int(fam["value"]) for fam in data}
    except (OSError, urllib.error.URLError, ValueError, KeyError, TypeError):
        return None
    _cache = (time.monotonic(), result)
    return dict(result)
