# Модель данных PricePulse

Локальный MVP использует SQLite. Схема версии `0001` определена в
`migrations/versions/0001_initial.py`. Основные таблицы: `sources`,
`source_policies`, `scraper_versions`, `products`, `variants`, `observations`,
`collection_jobs`, `job_results`, `schedules`, `alert_rules`, `alert_events`
и `data_quality_issues`.

## Домен

- `sources` — источник и активная версия.
- `source_policies` — политика, digest и флаг live режима.
- `scraper_versions` — candidate: revision, goal, hashes кода и зависимостей,
  provider/model, engine и fixture verification.
- `products` и `variants` — identity, title, canonical URL, SKU/variant и
  attributes.
- `observations` — наблюдение цены с `observed_at`, provenance, normalized value,
  status и quality flags.
- `collection_jobs` и `job_results` — идемпотентная работа и bounded результат.
- `alert_rules` и `alert_events` — правило, delta, cooldown и дедупликация.

## Цены

Деньги хранятся как decimal/text, а не binary float. Значение имеет валюту;
unit price может иметь unit name. Delta и threshold сравниваются только в одной
валюте. Quantity и unit не маскируются под основную цену.

## История

Триггеры `observations_append_only_update` и `observations_append_only_delete`
не дают изменять или удалять наблюдение. Correction добавляется новой записью с
отдельным provenance. Повторное сохранение одинакового результата защищено
ограничением:

```sql
UNIQUE (variant_id, observed_at, raw_hash, job_id)
```

## Идемпотентность

`collection_jobs` уникальны по `idempotency_key`; повторный запрос возвращает уже
созданный job. `job_results` уникальны по `(job_id, result_digest)`. Активация
кандидата разрешена только при статусе `validated` и пройденной fixture
verification; иначе API возвращает `409`.

## API и экспорт

История доступна по `GET /variants/{variant_id}/history` и экспортируется в CSV
или JSON. CSV-значения, начинающиеся с `=`, `+`, `-` или `@`, экранируются для
защиты от formula injection. Экспорт использует фиксированный набор полей истории.

## Целостность версий и URL

Метаданные `scraper_versions` неизменяемы: source/candidate, revision, goal,
hashes кода и зависимостей, policy digest, engine/provider/model и created_at.
Повторная запись с расхождением отклоняется без изменения сохранённой версии.
Для другого кода нужна новая версия и повторные gates. Upsert меняет только
status и fixture verification неизменного кандидата; активация выполняется
отдельно, а активную версию нельзя переписать обычным upsert.

Правила `drop_query_parameters` задаёт только SourcePolicy. Нормализация получает
эту политику отдельно от недоверенной товарной записи и проверяет source_id.
Без правил сохраняются все несекретные query-параметры; параметры варианта
удалять нельзя. Экспорт использует уже сохранённый canonical URL.
