# Платформа ОРВ · модуль П-10

Сервис анализирует нормативные акты Москвы и готовит Excel для эксперта: карту гипотез, запрос показателей, вопросы органу и вердикты. Эксперт проверяет результат; наличие файла или статус «готов» не доказывают правильность выводов.

```text
Акт → текст → паспорт → 12 индикаторов → гипотезы → карта → запрос → Excel
                                                                     ↓
                                                       диалог с экспертом
```

## Документация

- [Карта процесса](docs/PIPELINE.md) — входы, выходы и файлы каждого шага; инструменты ГАРАНТа, ограничения и ошибки.
- [История изменений](CHANGELOG.md) — что и когда менялось.
- [Архив предыдущего README](docs/archive/README_before_2026-11-09.md) — исторический снимок; команды и версии в нём могут быть устаревшими.
- Точные правила оценки — в [промптах](prompts/) и описаниях полей [контрактов](app/contracts.py). Документация указывает на них, не подменяя их.

## Где что находится

| Путь | Назначение |
|---|---|
| [app/web.py](app/web.py), [app/run_queue.py](app/run_queue.py) | Страница сервиса, вход в процесс и очередь прогонов. |
| [app/pipeline.py](app/pipeline.py), [app/nodes/](app/nodes/) | Порядок анализа и отдельные узлы. |
| [app/contracts.py](app/contracts.py), [app/input_contexts.py](app/input_contexts.py) | Схемы результатов и выбор частей паспорта для узлов. |
| [app/runner.py](app/runner.py), [app/config.py](app/config.py) | Вызов CLI, обработка ответа и настройки сервиса. |
| [app/mcp_garant.py](app/mcp_garant.py), [app/mcp_garant_web.py](app/mcp_garant_web.py), [scripts/garant_web.cjs](scripts/garant_web.cjs) | API ГАРАНТа и чтение интернет-версии. |
| [app/dialog.py](app/dialog.py), [app/garant_chat.py](app/garant_chat.py), [app/feedback.py](app/feedback.py) | Диалог по результату, ГАРАНТ-помощник и файл эксперта. |
| [prompts/](prompts/), [tests/](tests/), [deploy/](deploy/) | Инструкции моделей, тесты, конфигурации запуска. |
| `runs/`, `var/` | Результаты прогонов и служебные данные; не отправляются в Git. |

## Модели и настройки

Выбор движка задаётся **при запуске сервиса**, через `P10_LLM_BACKEND` в [app/config.py](app/config.py):

```text
codex  → gpt-6.1-terra, effort medium
claude → sonnet, effort medium
```

Если переменная не задана, код выбирает `claude`. На Spark production на дату выкладки 09.11.2026 запущен с `P10_LLM_BACKEND=codex`; актуальное состояние проверяйте в юните и метаданных прогона. Паспорт, индикаторы, гипотезы, карту, запрос и оба диалога выполняет выбранная LLM. Конвертация документа и сборка Excel выполняются кодом; Docling/OCR используются при извлечении текста и не выносят юридический вердикт.

`ActPassport`, `Indicator03Output` и другие классы в `contracts.py` — **модели данных**, а не LLM: они описывают и проверяют структуру ответа.

## Запуск и проверка

Нужны Python 3.12, зависимости из [requirements.txt](requirements.txt) и авторизованный CLI выбранного движка. Для веб-входа требуются `P10_AUTH_USER`, `P10_AUTH_PASSWORD`, `P10_STORAGE_SECRET` в окружении или локальном `.env.secrets`. Значения, токен ГАРАНТа, профиль браузера и файлы прогонов не включайте в репозиторий.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Настройте доступы и CLI в изолированной копии; порт 8030 должен быть свободен.
P10_LLM_BACKEND=codex .venv/bin/python -m app.web
```

Для API ГАРАНТа используется локальный токен `~/.config/garant/token`. Авторизованное веб-чтение требует отдельного браузера и действующей сессии; пример юнита — [p10-garant-browser.service](deploy/systemd-user/p10-garant-browser.service). Юнит в репозитории содержит пути Spark и перед установкой на другой машине требует проверки. Не записывайте логин, пароль или cookies в документацию.

На существующем Spark проверка состояния **без перезапуска**:

```bash
systemctl --user status p10-web.service p10-garant-browser.service p10-tuna.service p10-watchdog.timer
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8030/login
```

HTTP 200 подтверждает ответ страницы входа, но не работу модели или ГАРАНТа. Не перезапускайте сервис во время активных прогонов: очередь живёт в памяти процесса. Для анализа конкретного запуска используйте `runs/<run_id>/progress.json`, `run.log`, `events.jsonl` и [карту артефактов](docs/PIPELINE.md#артефакты-прогона).

Автоматические проверки без реальных вызовов модели:

```bash
.venv/bin/python -m pytest -q tests -m 'not live'
```
