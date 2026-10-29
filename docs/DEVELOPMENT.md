# Разработка PricePulse

## Окружение

- Python 3.11 или новее.
- [uv](https://docs.astral.sh/uv/) для воспроизводимой установки.
- Docker нужен для runtime-isolation tests; тесты data, API и UI не требуют его.

```sh
uv sync --locked --extra dev
uv pip install --requirement pricepulse/runtime/requirements.lock
```

Устанавливается lockfile базового пакета и отдельный product runtime lock. Не
заменяйте диапазоны зависимостей без обновления и проверки lockfile.

## Локальный control plane

```sh
uv run python -m pricepulse --database pricepulse.db --host 127.0.0.1 --port 8000
```

Модуль создаёт или открывает SQLite-файл, применяет миграции и запускает API
только на loopback. Dashboard доступен по корню, health — по `GET /health`.

## Локальные тесты

Контролируемые тесты используют fixtures, protocol doubles и временные SQLite
файлы. Полезные границы:

```sh
uv run --extra dev pytest -q tests/collectors
uv run --extra dev pytest -q tests/providers
uv run --extra dev pytest -q tests/data
uv run --extra dev pytest -q tests/api tests/jobs
uv run --extra dev pytest -q tests/ui
uv run --extra dev pytest -q tests/runtime
uv run --extra dev pytest -q tests/security
```

Для полного локального набора используйте runtime lock:

```sh
uv run --with-requirements pricepulse/runtime/requirements.lock --extra dev pytest -q
```

## Миграции

Миграции применяются в `Database.initialize()`. При изменении схемы создайте
новую версию `upgrade`/`downgrade`, не редактируйте уже применённую версию и не
удаляйте append-only ограничения. Валидируйте lifecycle миграций отдельным
фокусным тестом.

## Hardened runtime

Соберите локальный образ:

```sh
docker build -f Dockerfile.runtime -t pricepulse-runtime:local .
```

`pricepulse.runtime.SandboxedContainerRunner` по умолчанию ищет этот образ.
`deploy/runtime-compose.yaml` описывает read-only worker без сети и отдельный
egress broker. Broker требует JSON SourcePolicy в переменной
`PRICEPULSE_EGRESS_POLICY`. Не запускайте generated code без этих границ.

## Правила проверки

- Тесты используют fixtures, protocol doubles и временные SQLite-файлы.
- Реальные магазины, live LLM, CAPTCHA solvers, proxies и платные сервисы не
  вызываются автоматически.
- Секреты не читаются из `.env`, не создаются в файлах и не попадают в логи.
- Сохраняйте manifest происхождения исходников, MIT LICENSE и атрибуцию.
- Отсутствующая реализация или тест — defect, а не `pending_environment`.
