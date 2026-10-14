# kaggle-qwen-bridge

Free **Qwen3-Coder-30B** on a Kaggle GPU, reachable as a tool for Claude (or any MCP client).

Безкоштовна модель **Qwen3-Coder-30B** на GPU Kaggle, яку можна викликати інструментом із Claude
(або будь-якого MCP-клієнта). Українською нижче.

```
Claude ──MCP (stdio)──> bridge/qwen_mcp.py ──HTTPS──> Cloudflare quick tunnel ──> Kaggle kernel
                                                                                   ├─ token-checking proxy
                                                                                   └─ Ollama + qwen3-coder:30b
```

## Що тут є

| Файл | Призначення |
|---|---|
| `kernel/serve.py` | Ядро для Kaggle: ставить Ollama, тягне модель, ставить проксі з токеном, відкриває тунель, друкує адресу й токен у журнал |
| `kernel/kernel-metadata.json` | Метадані ядра (підстав свій логін Kaggle) |
| `bridge/connect.py` | Забирає адресу й токен із журналу ядра, зберігає їх локально; `--stop` вимикає ядро |
| `bridge/qwen_mcp.py` | MCP-сервер: інструменти `ask_qwen` і `qwen_status` |
| `tests/` | Офлайн-тести проксі та мосту (14 тестів, без Kaggle і GPU) |
| `docs/` | Налаштування Kaggle, швидкість і ліміти, підключення до Claude |

Усе — лише стандартна бібліотека Python. Нічого не ставиться через pip, крім самого Kaggle CLI.

## Швидкий старт

1. Kaggle: акаунт із підтвердженим телефоном, API-токен, `pip install kaggle` — [docs/01-kaggle-setup.md](docs/01-kaggle-setup.md).
2. У `kernel/kernel-metadata.json` заміни `YOUR_KAGGLE_USERNAME` на свій логін.
3. Запусти ядро:
   ```
   kaggle kernels push -p kernel --accelerator NvidiaTeslaT4
   ```
4. Через ~5–9 хвилин (черга, завантаження 18 ГБ, прогрів) забери адресу й токен:
   ```
   python bridge/connect.py ВАШ-ЛОГІН/qwen-serve
   ```
5. Підключи міст до Claude Code — [docs/03-claude.md](docs/03-claude.md):
   ```
   claude mcp add --scope user qwen -- python /шлях/до/bridge/qwen_mcp.py
   ```
6. Після роботи вимкни ядро (воно й само засинає за 10 хв простою):
   ```
   python bridge/connect.py --stop
   ```

## Що перевірено, а що ні

- Перевірено: ядро на 2× Tesla T4 піднімає модель і проходить власний самотест (авторизація, заборонені шляхи, tool-calls зі стрімом і без, тунель); міст `qwen_mcp.py` відповів на запит до живої моделі по протоколу MCP; 14 офлайн-тестів.
- Не перевірено: реєстрація мосту саме в Claude Code (команда з кроку 5 написана за документацією CLI); інші типи GPU; Linux/macOS (код кросплатформний, але запускався на Windows).

## Обмеження, про які варто знати

- Квота GPU Kaggle ≈ 30 год/тиждень; сесія до ~9 год; ядро само вимикається після 10 хв без запитів.
- Генерація ≈ 55 токенів/с на 2× T4, тобто довга відповідь займає десятки секунд — [docs/02-speed-and-limits.md](docs/02-speed-and-limits.md).
- **Дані йдуть на Kaggle** (сервери третьої сторони) через тунель Cloudflare. Не надсилай моделі секрети й чутливі дані.
- Адреса тунелю нова при кожному старті ядра; журнал ядра містить токен, тому ядро має лишатись приватним.
- Деякі мережі блокують `*.trycloudflare.com` — [docs/04-blocked-network.md](docs/04-blocked-network.md).
