# Аудит пригодности ScrapeClaw для PricePulse

Источник: YunyiFlux/ScrapeClaw, закреплённый commit в source manifest. Проверены
фактический MIT LICENSE, pyproject.toml и исходники; рекламное production-ready
не считается результатом тестов.

| Область | Факт по коду | Действие для PricePulse |
| --- | --- | --- |
| Probe/synthesis/export | SDK/CLI, Playwright, goal/traffic analysis, scaffolders, CSV/JSON/JSONL | Переиспользовать через adapter после baseline |
| Execution gate | asyncio child process, env наследуется из os.environ; standalone path не доказывает OS isolation | Заменить интеграцию hardened worker; host fallback запрещён |
| AST checker | Проверяет некоторые вызовы и imports, не sandbox | Оставить как дополнительный preflight |
| Проверка успешности | Есть ветки Scrapy/Drission static-only pass без extracted data | Ввести отдельные static/runtime/data verdicts |
| Providers | OpenAI SDK/OpenAI-compatible protocol | Добавить и протестировать native Anthropic/Ollama adapters |
| Тесты/воспроизводимость | В tree нет tests и lockfile; pyproject допускает диапазоны версий | Снять реальный baseline, добавить lock и независимые fixtures |
| Product layer | Нет подтверждённых PricePulse DB/history/jobs/API/dashboard | Реализовать как отдельные зоны |

Подготовка не исполняла сгенерированные скраперы, не обращалась к реальным целям,
не использовала live LLM/solver accounts и не проверяла runtime isolation. Для
таких результатов нужны отдельные реальные evidence. Возможность сбора каждой
площадки проверяется отдельно, с ограничениями источника и сети.

Гигиена публичного среза: исходный .gitignore исключал все docs/ и tests/. Эти
две чрезмерно широкие маски убраны, чтобы provenance, документация и будущие
продуктовые тесты оставались в Git. Остальные исходники ScrapeClaw не менялись.
