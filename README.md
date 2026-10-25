# PricePulse — мониторинг цен и товарных данных

Платформа на основе [ScrapeClaw](https://github.com/YunyiFlux/ScrapeClaw):
цель и разрешённый URL → скрапер-кандидат → изолированная проверка → сбор →
нормализация → история цен, уведомления и экспорт.

**Статус: подготовлены исходники и проектная структура. Предметный MVP ещё не реализован.**
Описание ниже задаёт направление продукта, а не утверждает готовность функций.

## Планируемые возможности

- Версионированная генерация скраперов по URL и описанию цели.
- Source policies, local SPA fixtures, проверка и явная активация кандидатов.
- Пайплайн collect → normalize → deduplicate → enrich → store.
- Точные денежные значения, варианты товаров, availability и provenance.
- История и изменения цен, идемпотентные уведомления и durable jobs.
- CSV/JSON/API и локальный dashboard.
- Адаптеры OpenAI, Anthropic и Ollama с contract-тестами без реальных ключей.

Каждая площадка подключается и проверяется отдельно. Поддержка произвольного
сайта и успешное преодоление любых блокировок не гарантируются.

## Основа и граница переиспользования

ScrapeClaw 1.0.0, MIT, закреплённый commit:
`4e0ed78478db38c1173610b39dba573dcf46bd77`.

Переиспользуются SDK/CLI, browser probe, анализ SPA/traffic, scaffolders и exporters.
Исходный execution gate — subprocess с окружением хоста; он не является готовым
изолированным runtime для сервиса. Hardened runtime, очереди/jobs, product DB,
API/dashboard и provider adapters требуют реализации и отдельной проверки.

[Исходная документация](docs/upstream/README.md) ·
[Проверенные возможности и ограничения](docs/UPSTREAM_ASSESSMENT.md) ·
[Manifest исходников](docs/upstream/source_manifest.json)

## Структура

```text
scrapeclaw/       # Закреплённое ядро upstream
pyproject.toml   # Исходный manifest пакета ScrapeClaw
LICENSE          # Оригинальная MIT-лицензия
ATTRIBUTION.md
config.example.yaml
docs/upstream/   # Исходные README и provenance
```

Модули PricePulse, миграции, API, runtime и tests появятся при реализации.
Будущий локальный MVP: Python, SQLite, небольшой HTTP API/dashboard; окончательные
команды запуска будут зафиксированы после baseline и реализации.

## Установка исходного ScrapeClaw

Исходный пакет требует Python 3.10+, Playwright/Chromium; для будущего PricePulse
планируется Python 3.11+. Инструкции upstream описаны в его README, но исходники
в этом срезе ещё не проходили продуктовую проверку окружения. Установка SDK
не означает наличие работающего PricePulse API или безопасного execution worker.

Реальные сайты, LLM API и сгенерированные скраперы во время подготовки не запускались.
Рабочие credentials не входят в репозиторий. Не запускайте недоверенный generated
код на хосте; проверенный изолированный runtime — обязательное условие исполнения.

## Лицензия

ScrapeClaw: **MIT**, copyright (c) 2026 ScrapeClaw Contributors.
[LICENSE](LICENSE) и [атрибуция](ATTRIBUTION.md) сохранены.
