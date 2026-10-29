# Troubleshooting, blocked state и quarantine

## SourcePolicy отклонена

`400` при `POST /sources` означает отказ валидации SourcePolicy. Проверьте точные
hosts, абсолютные пути без traversal, robots policy, лимиты, валюту и
`live_enabled`. Не изменяйте код, чтобы принять wildcard, внутренний host,
credential или live режим без одобрения.

## Кандидат не активируется

`POST /sources/{source_id}/{version_id}/activate` возвращает `409`, если candidate
не находится в статусе `validated` или fixture verification не пройдена. Candidate
не подменяет активную версию. Для регрессии создаётся новая версия; при
подтверждённой неисправности применяется `quarantined`.

## Jobs и идемпотентность

Повторный `POST /jobs` с тем же `idempotency_key` не создаёт второй job. Если
превышен локальный concurrency limit, API возвращает `429`. Статусы: `queued`,
`running`, `succeeded`, `failed`, `blocked` и `cancelled`. Отменить можно только
queued job. Истекший lease восстанавливается, а не выполняется вторым владельцем.

## Runtime недоступен

`RuntimeUnavailableError` означает, что hardened runtime недоступен. Не
запускайте generated code на хосте. Для runtime-проверок нужен доступ к Docker CLI
и образ `pricepulse-runtime:local`. Контейнер остаётся non-root, read-only и без
сети.

## Challenge и rate limit

HTTP `401`, `403`, `429` и challenge обрабатываются как blocked state. Не обходите
их, не повышайте rate и не расширяйте allowlist. Идемпотентные ошибки
повторяются только в пределах `retry_budget`; повторный cooldown завершает job.

## Качество и история

Data quality issues хранятся отдельно от наблюдений. Normalize, dedupe и enrich
не исправляют append-only наблюдение задним числом. Новое наблюдение или
отдельная корректировка сохраняет исходный provenance.

## Экспорт

CSV-значения, начинающиеся с `=`, `+`, `-` или `@`, экранируются. Если экспорт
возвращает ошибку, проверьте существование variant и `format=csv` или `json`. Не
отключайте sanitizer.
