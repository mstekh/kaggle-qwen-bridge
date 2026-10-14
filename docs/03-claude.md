# 3. Підключення до Claude

`bridge/qwen_mcp.py` — MCP-сервер по stdio. Дає два інструменти:

- `ask_qwen(prompt, system?, max_tokens?, temperature?)` — запит до Qwen; повертає текст.
- `qwen_status()` — чи модель онлайн.

Модель не має пам'яті й власних інструментів: Claude має покласти весь потрібний контекст у `prompt`.

## Claude Code

```
claude mcp add --scope user qwen -- python /шлях/до/kaggle-qwen-bridge/bridge/qwen_mcp.py
```

Статус: команда складена за документацією `claude mcp add`; саме в Claude Code автор міст не запускав.
Протокол перевірено вручну (initialize → tools/list → tools/call) проти живої моделі та автотестами `tests/test_mcp.py`.

Інші MCP-клієнти: запускати `python bridge/qwen_mcp.py` як stdio-сервер.

## Звідки міст бере адресу

Щоразу, коли викликається інструмент, заново читає:

1. змінні `QWEN_URL`, `QWEN_TOKEN`, `QWEN_MODEL`, якщо `QWEN_URL` задано;
2. інакше `~/.config/kaggle-qwen-bridge/endpoint.json`, який пише `bridge/connect.py`.

Адреса тунелю змінюється при кожному старті ядра, тож після нового запуску досить повторити `connect.py`;
перезапускати Claude не треба.

## Типові відповіді-помилки

| Повідомлення | Причина |
|---|---|
| «Немає адреси моделі…» | ядро не запускалось або `connect.py` не виконано |
| «Модель недоступна…» | ядро заснуло (idle 10 хв) або тунель змінився: `kaggle kernels push`, потім `connect.py` |
| «HTTP 401…» | токен із минулого запуску: повторити `connect.py` |

## Безпека

Усе, що Claude передає в `ask_qwen`, виходить на Kaggle. Токен лежить у `endpoint.json` з правами лише власнику
і не логується мостом. Інструмент не дає моделі жодного доступу до твоїх файлів чи команд.
