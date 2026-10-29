# Changelog

Формат основан на Keep a Changelog; проект пока не использует semantic versioning.

## [Unreleased] — 2026-10-29

### Changed

- Используемое ядро интегрировано в `pricepulse/collectors` с единым стилем кода
  и Unicode-escaped сигнатурами challenge; неиспользуемые legacy-модули удалены.
- PricePulse устанавливается собственным wheel с CLI, миграциями и web-assets.
- Оригиналы upstream сохранены в проверяемом архиве с неизменным manifest и MIT notice.

### Added

- Локальный MVP мониторинга цен: source onboarding, кандидаты скраперов,
  fixture-верификация, offline gate, hardened Docker runtime и egress broker.
- SQLite-схему и миграции для источников, политик, версий, товаров, вариантов,
  append-only наблюдений, jobs, алертов и quality issues.
- Collect → normalize → deduplicate → enrich → store, идемпотентные jobs и
  идемпотентные алерт-события.
- Loopback REST API, локальный dashboard и безопасный CSV/JSON-экспорт.
- Протокольные двойные адаптеры для OpenAI, native Anthropic и Ollama.

### Changed

- Публичная документация синхронизирована с реализованным локальным MVP.
- Upstream execution gate остаётся preflight-механизмом, а не продуктовым runtime.

### Pending

- Внешний smoke на оператором утверждённой публичной цели.
- Live-провайдеры OpenAI, Anthropic и Ollama с записанными лимитами и redaction.
