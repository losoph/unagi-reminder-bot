## MS3 Распил main.py: экспорт/импорт данных в Router

- **Решение**: Вынести кластер экспорта и импорта пользовательских данных (`/export`, `/import`, FSM `ImportState`, `_send_export`, `_prompt_import`, `_import_user_payload`, хендлеры `cb_export`, `cb_import`, `cb_import_cancel`, `process_import_file`, `import_wrong_type` и сопутствующие константы `MAX_IMPORT_*`, `EXPORT_VERSION`) в автономный модуль `data_transfer.py` с собственным `Router(name="data_transfer")`. Роутер зарегистрирован в `dp.sub_routers` до `fallback_router`. Для полной обратной совместимости все символы реэкспортированы в `main.py`, а экземпляр `bot` внедряется через `configure(bot=bot)`.
- **Изоляция и обратная совместимость**: `data_transfer.py` не импортирует `main` (проверено тестом `test_guard_no_main_imports_in_data_transfer`). Все существующие тесты экспорта/импорта работают без изменений через `main.<symbol>` и напрямую через `data_transfer`.
- **Сверка порядка диспетчеризации (`tools/handler_order.py compare`)**:
```
набор хендлеров тот же: 150
перенесено в другой роутер: 7
[callback_query] пар с возможной сменой приоритета: 0
[message] пар с возможной сменой приоритета: 0
[update] пар с возможной сменой приоритета: 0
ИТОГ: порядок не конфликтует
```
- **Тесты**: Добавлен набор тестов `tests/test_data_transfer.py` (архитектурные проверки изоляции, порядка включения роутеров, приоритетов хендлеров перед fallback, функциональные тесты команд `/export`, `/import`, отмены, валидации форматов и полного цикла roundtrip).
