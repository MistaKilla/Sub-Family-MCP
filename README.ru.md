# Sub-Family

[English](README.md) | **Русский**

[![CI](https://github.com/MistaKilla/Sub-Family-MCP/actions/workflows/ci.yml/badge.svg)](https://github.com/MistaKilla/Sub-Family-MCP/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sub-family-mcp.svg)](https://pypi.org/project/sub-family-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/sub-family-mcp.svg)](https://pypi.org/project/sub-family-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<!-- mcp-name: io.github.MistaKilla/sub-family -->

Sub-Family — это MCP-сервер, который превращает список локальных и облачных
LLM из YAML-файла в семью делегатов для Claude Code: с постоянными сессиями,
ограниченным параллельным вызовом, поддержкой изображений и маскированием
секретов с учётом уровня доверия.

```text
Claude Code / any MCP client
        │  MCP over stdio
        ▼
  sub-family-mcp
  ├── registry ...... family.yaml (members, trust, limits)
  ├── sessions ...... SQLite history per (member, session_id)
  ├── guard ......... secret redaction, hard blocks, image policy
  └── HTTP client ... OpenAI-compatible Chat Completions
        ├── local farm: Ollama, LM Studio, vLLM, llama.cpp server
        └── cloud APIs: DeepSeek, OpenAI, OpenRouter, any gateway
```

## Зачем это нужно

Claude Code хорошо планирует и проверяет результат, но часть работы не стоит
выполнять на основной модели или на новых локальных субагентах:

- **Локальная ферма и так загружена.** Ещё один локальный субагент борется за
  тот же GPU и тот же контекст. Sub-Family отправляет целую пачку задач одному
  участнику — локальному или облачному — с жёстким лимитом параллельности,
  поэтому параллельная рассылка не кладёт машину.
- **Субагент тяжелее обычного вызова модели.** Встроенный субагент — это
  отдельный агентный цикл со своими инструментами и контекстом. Участник семьи —
  это прямой запрос к чат-модели: дешевле, быстрее и предсказуемее, а итоговое
  решение остаётся за основным агентом.
- **Не всё можно отдавать в облако.** Скриншоты, закрытый код и черновики
  должны оставаться на вашем железе; обзор архитектуры и большой аудит можно
  поручить облачной модели. Уровни доверия делают этот выбор явным, а
  маскирование секретов срабатывает до того, как запрос покинет машину.

## Возможности

- **YAML-реестр** (по умолчанию `~/.config/sub-family/family.yaml`, в Windows —
  `%APPDATA%\sub-family\family.yaml`) со строгой проверкой: модели, базовые URL,
  уровни доверия, возможности, таймауты и лимит параллельности для каждого
  участника.
- **Семь MCP-инструментов**: посмотреть состав семьи, проверить доступность,
  спросить одного участника, запустить рой, показать сессии, прочитать историю,
  сбросить сессию.
- **Постоянные сессии** в SQLite с ключом `(member, session_id)`, скользящим
  окном и сохранением после перезапуска.
- **Ограниченный параллельный вызов (рой)** через `ask_family_swarm`: сбой
  одной ветки не отменяет вызов, а необязательный синтез собирает успешные
  ответы в один.
- **Изображения** из локальных файлов, `http(s)`-URL и base64 data URI — только
  для участников с возможностью `vision`.
- **Безопасность с учётом доверия**: маскирование секретов для недоверенных
  облачных участников, пользовательские жёсткие блокировки для всех, политика
  для изображений и лимиты длины запроса.
- **Без SDK вендоров**: один адаптер на `httpx` покрывает любой
  OpenAI-совместимый endpoint, а повторы, таймауты, TLS и `extra_body` остаются
  под явным контролем.
- **CLI и TUI**: скриптуемые команды `sub-family` и полноэкранный редактор
  участников на Textual.
- **Только stdio**: без демона, без слушающего порта и без сервера, который
  нужно поддерживать.

Нужны Python 3.11+, официальный MCP Python SDK 2.x и OpenAI-совместимый
endpoint модели. Лицензия MIT.

## Быстрый старт

### 1. Установка

```bash
# uv: https://docs.astral.sh/uv/
uv tool install sub-family-mcp       # installs `sub-family` and `sub-family-mcp`
uvx sub-family-mcp --version         # or run once without installing
```

`uvx sub-family-mcp` запускает MCP-сервер. Административные команды используют
точку входа `sub-family`, которую устанавливает `uv tool install`.

Установка для разработки:

```bash
git clone https://github.com/MistaKilla/Sub-Family-MCP.git
cd Sub-Family-MCP
uv sync --extra dev
uv run sub-family init
```

### 2. Создание конфигурации

```bash
sub-family init
```

Команда записывает прокомментированный стартовый файл с двумя участниками:

- `big_brother` — DeepSeek `deepseek-chat` через `DEEPSEEK_API_KEY`, уровень
  доверия `untrusted_cloud`.
- `lilbro` — локальная vision-модель Qwen по адресу
  `http://192.168.1.50:11434`. Замените адрес на реальную машину или удалите
  участника.

`sub-family init` не перезаписывает существующий файл без `--force`; при
перезаписи сохраняется копия `.bak`.

### 3. Редактирование участников

Через TUI:

```bash
sub-family ui
```

Или добавьте участника флагами:

```bash
sub-family members add farm_two \
  --base-url http://192.168.1.51:11434 \
  --model qwen2.5-coder:7b \
  --trust local \
  --capability code \
  --capability fast
```

Имена участников — строчные, начинаются с буквы и могут содержать цифры, `_`
и `-`.

### 4. Ключи API

Предпочтительны переменные окружения:

```bash
export DEEPSEEK_API_KEY="sk-..."
```

Или положите ключи в `~/.config/sub-family/.env`: CLI и сервер читают этот файл
автоматически и не переопределяют уже экспортированные переменные.

```bash
# ~/.config/sub-family/.env
DEEPSEEK_API_KEY=sk-...
```

```bash
chmod 600 ~/.config/sub-family/.env
```

Локальным endpoint вроде Ollama ключ обычно не нужен. О том, как работают
`api_key_env`, собственные заголовки и ключи, записанные прямо в конфигурации,
читайте в [Configuration](docs/configuration.md).

### 5. Проверка доступности

```bash
sub-family status
```

`status` опрашивает `/models` каждого участника, не тратя токены на генерацию.
Если включённый участник недоступен, команда завершается с ненулевым кодом. Для
одного участника или машинного вывода добавьте `--member lilbro` или `--json`.

### 6. Подключение Claude Code

```bash
claude mcp add --scope user sub-family -- uvx sub-family-mcp
```

Перезапустите Claude Code и попросите его вызвать `list_family`:

```text
Use list_family and tell me which models are available and what they are good at.
```

Если конфигурация лежит в нестандартном месте, передайте путь после имени
пакета:

```bash
claude mcp add --scope user sub-family -- \
  uvx sub-family-mcp --config /absolute/path/to/family.yaml
```

Описание проектного `.mcp.json`, подстановки переменных и готовых сценариев
делегирования см. в [Claude Code integration](docs/claude-code.md).

## Минимальная конфигурация

```yaml
version: 1

members:
  big_brother:
    description: "Cloud heavyweight for architecture, hard debugging and code review."
    base_url: "https://api.deepseek.com"
    model: "deepseek-chat"
    api_key_env: "DEEPSEEK_API_KEY"
    trust: "untrusted_cloud"
    capabilities: ["reasoning", "code", "long_context"]
    max_clones: 5
    timeout_s: 300
    max_tokens: 8192

  lilbro:
    description: "Small local vision model for screenshots, diagrams and quick OCR."
    base_url: "http://192.168.1.50:11434"
    model: "qwen2.5-vl:7b"
    trust: "safe_local"
    capabilities: ["vision", "fast", "chat"]
    max_clones: 1
    timeout_s: 120
    max_tokens: 2048

sessions:
  enabled: true
  max_messages: 40
  max_chars: 80000
  ttl_days: 30

security:
  redact_secrets: true
  redact_trust: ["untrusted_cloud"]
  deny_images_to_remote: false
  max_image_mb: 20
  expose_reasoning: false

swarm:
  max_tasks: 12
  max_result_chars: 12000
  default_synthesize: false
```

Полный прокомментированный шаблон лежит в
[`examples/family.example.yaml`](examples/family.example.yaml) и создаётся той же
заготовкой, что и `sub-family init`. Все поля, значения по умолчанию, правила
URL и примеры для бэкендов описаны в
[Configuration](docs/configuration.md).

## Инструменты

| Инструмент | Что делает | Когда использовать |
| --- | --- | --- |
| `list_family` | Возвращает участников, модели, доверие, возможности, размер контекста, лимиты параллельности и состояние. | Один раз за сессию, перед выбором делегата. `include_disabled` показывает скрытых участников. |
| `family_status` | Опрашивает `/models` каждого участника: задержка, доступность, наличие настроенной модели. | Перед большим роем, после сбоя или если локальная машина могла уйти в офлайн. |
| `ask_family` | Отправляет один запрос одному участнику; опционально сессия, изображения, system prompt, temperature и лимит токенов. | Одна чётко очерченная задача: обзор, второе мнение, разбор скриншота, точечная генерация. |
| `ask_family_swarm` | Параллельно выполняет независимые запросы к одному участнику в пределах `max_clones`; опционально синтез. | Три и более независимых подзадач: аудит пяти модулей, тесты для нескольких файлов, сравнение подходов. |
| `list_sessions` | Показывает сохранённые сессии, сначала новые, с числом сообщений и превью. | Найти идентификатор прошлой сессии или проверить, есть ли у ветки история. |
| `session_history` | Возвращает последние сообщения одной сессии. | Восстановить точный контекст. Обычно лучше дать `ask_family` самому переиграть историю: чтение съедает контекст. |
| `reset_session` | Удаляет одну сессию или все сессии участника через `*`. | Задача закончена, и устаревший контекст не должен попасть в следующую работу. |

Основные аргументы:

- `ask_family`: `member`, `prompt`, `session_id`, `images` (до 8), `system`,
  `temperature`, `max_tokens`.
- `ask_family_swarm`: `member`, `tasks` (до `swarm.max_tasks`), `session_prefix`,
  `max_concurrency`, `context`, `synthesize`.
- Идентификатор сессии — от 1 до 64 символов из букв, цифр, `.`, `_`, `:`, `-`;
  начинается с буквы или цифры.

Сбой вызова участника возвращается как ошибка MCP-инструмента с понятным
описанием. Сбой ветки роя становится результатом `{ok: false, error: ...}` для
этой ветки, остальные ветки продолжают работу.

## Сессии и рой

Сессия — это разговор на стороне сервера с одним участником. Основной модели не
нужно тащить весь транскрипт:

```text
You:   Use ask_family with member big_brother and session_id "billing-refactor":
       review src/billing/parser.py for rounding and idempotency bugs.

Claude: [calls ask_family; gets a review]
You:   Continue the same session. Propose a minimal patch for the top issue.
```

Второй запрос переигрывает первый обмен из SQLite на сервере. Claude видит
только новый ответ, поэтому его контекст остаётся компактным. Сессии переживают
перезапуск MCP-сервера и Claude Code, изолированы по участникам и обрезаются до
`sessions.max_messages` / `sessions.max_chars`. Законченную сессию сбрасывайте
через `reset_session`.

Рой нужен для по-настоящему независимой работы:

```text
You:   Use ask_family_swarm with member big_brother, session_prefix "audit-may",
       tasks:
       1. Review src/auth for injection and session fixation.
       2. Review src/billing for rounding and idempotency bugs.
       3. Review migrations/ for destructive changes.
       4. Review the HTTP retry logic for duplicate writes.
       5. Review the Dockerfile for secrets and root user.
       Use synthesize: true and return one prioritized report.
```

Что происходит:

- Пять запросов идут параллельно, но не больше, чем позволяет `max_clones`
  участника `big_brother`.
- Ветка *N* получает сессию `audit-may-N`, поэтому каждую ветку можно продолжить
  позже.
- Сбой одной ветки не отменяет вызов: в её результате будут `ok: false` и
  `error`.
- `synthesize: true` добавляет один запрос, который объединяет успешные ответы.
- Содержимое каждой ветки обрезается до `swarm.max_result_chars` (по умолчанию
  12 000 символов), прежде чем оно попадёт к основной модели.

## Доверие и безопасность

| Уровень доверия | Для чего | Маскирование по умолчанию | Изображения |
| --- | --- | --- | --- |
| `safe_local` | Модель на вашем железе. | Нет | Разрешены. |
| `private_network` | Удалённый endpoint, который вы контролируете. | Нет | Разрешены с предупреждением. |
| `untrusted_cloud` | Сторонний API. | Шаблоны секретов заменяются. | Разрешены с предупреждением; можно запретить. |

Перед отправкой участнику `untrusted_cloud` guard маскирует известные формы
учётных данных (приватные ключи, ключи `sk-`, токены GitHub/AWS/Google/Slack,
JWT, bearer-токены, пароли в URL и типичные присваивания в стиле `.env`) и
сообщает, что именно изменено. `security.redact_trust` распространяет это
правило на другие уровни.

`security.blocked_patterns` — более сильный инструмент: пользовательские
регулярные выражения проверяются для всех участников, включая локальных, и при
совпадении вызов отклоняется.

Это страховка, а не песочница. Маскирование пропускает новые форматы, короткие
ключи, секреты, разнесённые по строкам, и всё, что находится внутри изображения.
Никогда не вставляйте настоящие учётные данные в запрос, полагаясь на guard.
Полная [модель безопасности](docs/security.md) — в отдельном документе.

## Обзор CLI

Глобальная опция: `--config`/`-c` указывает на нестандартный `family.yaml`.

| Команда | Назначение |
| --- | --- |
| `sub-family init [--force]` | Записать прокомментированный стартовый конфиг. |
| `sub-family ui` | Открыть редактор участников на Textual (нужен TTY). |
| `sub-family status [--member NAME] [--json]` | Проверить доступность; код возврата 1 при сбое. |
| `sub-family doctor [--network]` | Проверить конфиг, хранилище, ключи и при желании endpoint. |
| `sub-family list [--json]` | Показать настроенных участников. |
| `sub-family members show/add/edit/remove/enable/disable/test` | Управление участниками. `test` проверяет одного. |
| `sub-family config path/show/validate/edit` | Посмотреть или изменить конфиг; `edit` проверяет перед сохранением. |
| `sub-family sessions list/show/reset/prune` | Просмотр и очистка истории SQLite. |
| `sub-family claude snippet` / `claude guide` | Вывести JSON для MCP-регистрации и блок делегирования для `CLAUDE.md`. |
| `sub-family paths [--json]` | Показать итоговые пути конфига, `.env`, данных и базы. |
| `sub-family serve [--log-level]` | Запустить MCP-сервер через stdin/stdout. |
| `sub-family version` | Показать установленную версию. |

Все команды пригодны для скриптов и печатают ошибки без traceback. `members
add` принимает повторяющийся `--capability`; `members edit` меняет только
переданные поля. Полные опции — в [Configuration](docs/configuration.md) или в
`sub-family <command> --help`.

## Поддерживаемые бэкенды

Участником может стать любой endpoint, реализующий OpenAI Chat Completions.

| Бэкенд | Пример `base_url` | Замечания |
| --- | --- | --- |
| Ollama | `http://192.168.1.50:11434` | `/v1` добавляется автоматически. Сначала загрузите модель. |
| LM Studio | `http://192.168.1.50:1234/v1` | Запустите локальный сервер и загрузите модель. |
| vLLM | `http://192.168.1.60:8000/v1` | `model` должен совпадать с именем обслуживаемой модели. |
| llama.cpp server | `http://192.168.1.70:8080/v1` | Используйте OpenAI-совместимый режим сервера. |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` или `deepseek-reasoner`. |
| OpenAI | `https://api.openai.com/v1` | Обычный API-ключ. |
| OpenRouter | `https://openrouter.ai/api/v1` | Берите идентификатор модели из каталога. |
| Другие шлюзы | Ваш OpenAI-совместимый корень | Для особенностей есть `chat_path`, `headers`, `extra_body`. |

Правила построения URL и готовый YAML для каждого бэкенда — в
[Configuration](docs/configuration.md#backend-examples).

## Быстрая диагностика

| Симптом | Первый шаг |
| --- | --- |
| `configuration not found` | `sub-family init` или укажите файл через `--config` / `SUB_FAMILY_CONFIG`. |
| HTTP 401 или 403 | Проверьте `api_key_env` в окружении, из которого запущен сервер; проверьте заголовок и префикс. |
| HTTP 404 или `model_available: false` | Исправьте `base_url`; загрузите точный идентификатор модели из `/v1/models`. |
| Connection refused или таймаут | Убедитесь, что endpoint слушает не только loopback; проверьте файрвол и `timeout_s`. |
| Запросы уходят не по тому пути | Задайте `chat_path` или уберите лишний `/v1` из `base_url`. |
| Изображения отклоняются | Добавьте `vision`; проверьте формат, размер (`max_image_mb`) и лимит в 8 изображений. |
| Текст изменился перед отправкой | Посмотрите `warnings` и `redactions`; настройте `redact_trust` или используйте локального участника. |
| `ui` падает в pipe или CI | Нужен TTY; используйте `sub-family members add` или `config edit`. |
| `uvx: command not found` | Установите uv либо используйте `uv tool install` / `pipx install` и запускайте `sub-family-mcp`. |

Точные сообщения и способы исправления — в
[Troubleshooting](docs/troubleshooting.md).

## Разработка

```bash
git clone https://github.com/MistaKilla/Sub-Family-MCP.git
cd Sub-Family-MCP
uv sync --extra dev

uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

Тесты используют `pytest-asyncio`, `respx` для подмены HTTP и MCP-клиенты
в памяти; живой endpoint модели не нужен. Textual TUI исключён из строгой
проверки mypy в `pyproject.toml`.

Соглашения разработки — в [CONTRIBUTING.md](CONTRIBUTING.md), границы слоёв и
поток запроса — в [Architecture](docs/architecture.md).

## Документация

- [Configuration](docs/configuration.md) — все поля, пути, уровни доверия,
  примеры бэкендов, резервное копирование.
- [Architecture](docs/architecture.md) — слои, потоки запросов, параллельность,
  схема SQLite, добавление провайдера.
- [Security](docs/security.md) — модель угроз, правила маскирования, политика
  изображений, хранение ключей, сообщение об уязвимостях.
- [Claude Code integration](docs/claude-code.md) — регистрация, `CLAUDE.md`,
  сценарии, стоимость контекста, отладка запуска.
- [Troubleshooting](docs/troubleshooting.md) — частые ошибки и решения.
- [Publishing](docs/publishing.md) — чек-лист релиза, PyPI Trusted Publishing,
  MCP Registry.
- [CHANGELOG.md](CHANGELOG.md) — история релизов.
- [SECURITY.md](SECURITY.md) — поддерживаемые версии и приватный контакт.

## Лицензия

MIT. См. [LICENSE](LICENSE).
