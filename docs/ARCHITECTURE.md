# Архитектура PricePulse

PricePulse разделяет control plane и исполнение недоверенного сгенерированного
кода. Control plane валидирует источники и политику, хранит данные и историю,
управляет jobs, алертами и API. Generated worker работает в отдельном
Docker-контейнере без сети и host-секретов. Live-сеть доступна только через
отдельный egress broker.

```text
SourcePolicy → onboarding → candidate
candidate → fixture verification → execution/data gates → activation
activation → job → hardened worker → bounded capture
capture → normalize → dedupe → enrich → SQLite append-only store
store → history, alerts, exports, dashboard
```

## Компоненты

| Компонент | Назначение |
| --- | --- |
| Collectors | Валидация SourcePolicy, local fixtures, probe/synthesis, offline gate, sanitizer и prompt guard |
| Providers | OpenAI-compatible, native Anthropic и Ollama адаптеры со строгой схемой |
| Runtime | `ExecutionRequest`/`ExecutionResult`, лимиты протокола и `SandboxedContainerRunner` |
| Egress | Hosts, пути, redirects, rate, concurrency, timeout и размер ответа |
| Domain | Source, политика, версия скрапера, товар, вариант, наблюдение, job, алерт, quality issue |
| Data | SQLite, миграции, normalize, dedupe, enrich, repository |
| Jobs | Идемпотентный запуск, lease, retry/backoff, отмена и восстановление истекших lease |
| API/UI | Loopback REST API, статический dashboard, экспорт CSV/JSON |

## Границы безопасности

- Generated script не исполняется control-plane или на хосте напрямую.
- Worker получает только переменные `PRICEPULSE_JOB_*`, bounded stdin,
  bounded stdout/stderr, timeout и отдельный `/tmp`.
- Контейнер запускается от UID/GID `65532` с `--read-only`, `--network=none`,
  `--cap-drop=ALL`, `no-new-privileges`, read-only `/dev/shm` и лимитами ресурсов.
- Broker принимает только `GET`/`HEAD`, требует `live_enabled=true`, ограничивает
  redirects, rate, concurrency, hosts, paths и размер ответа.
- HTTP `401`, `403`, `429` и challenge остаётся typed blocked state; fallback обхода
  отсутствует.
- Cookies, Authorization, credentials и личные данные не передаются в LLM, логи
  или evidence.

## Консистентность

SQLite хранит append-only наблюдения и защищает повтор от записи уникальным
индексом `(variant_id, observed_at, raw_hash, job_id)`. Jobs уникальны по
`idempotency_key`. Активация выполняется только для validated candidate после
execution и data gates; неудачный candidate не подменяет активную версию.

## Отложенные внешние этапы

Локальная реализация прошла fixture, security и integration checks. Оператор
должен отдельно утвердить публичную цель для bounded live smoke и предоставить
real provider accounts для live OpenAI, Anthropic и Ollama smoke. Эти этапы не
считаются пройденными без записанного внешнего evidence.
