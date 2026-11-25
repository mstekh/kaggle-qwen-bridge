# kaggle-ollama-bridge

Локальний **Ollama/OpenAI-сумісний endpoint** на `127.0.0.1:11434`, за яким насправді
стоїть **Qwen у Kaggle**. Звичайна Windows-програма Ollama (`ollama list`, `ollama run`)
і будь-який OpenAI-клієнт працюють без змін — локальні ваги розгортати не треба.

Це **тонка надбудова** над наявним `Desktop/Projects/kaggle-projects/claude-kaggle-bridge`.
Qwen піднімає й тримає його шлюз (`kagent-gateway` на vps-nl); міст лише дає стабільний
локальний порт 11434 і підставляє автентифікацію.

```
ollama / curl / OpenAI-клієнт ──> bridge.py (127.0.0.1:11434)
                                       │  читає ~/.config/kagent/config.json (url+key+CA)
                                       ▼  HTTPS + Bearer, через Tailscale/публічний IP
                              kagent-gateway на vps-nl (185.253.219.222)
                                       ▼  стартує/проксіює ядро
                              Kaggle: Ollama + Qwen3-coder:30b (2× T4)
```

**Чому через шлюз, а не напряму на Kaggle:** мережа bbeast блокує Cloudflare quick-tunnel
Kaggle (TCP до tunnel-IP таймаутиться), а VPS його досягає. Тому трафік іде bbeast → VPS
(Tailscale/HTTPS, не фільтрується) → Kaggle.

## Файли
| Файл | Призначення |
|---|---|
| `bridge.py` | проксі 11434 -> шлюз, стрімінг, підстановка токена, health `/__bridge/health`, локальний `/` і `HEAD /` як справжній Ollama |
| `config.json` | режим `kagent-gateway` за замовчуванням |
| `start_bridge.bat` | запуск моста на Windows |

## Щоденний цикл
1. **Підняти модель на Kaggle** (через шлюз; ~5–6 хв):
   ```
   python <...>/claude-kaggle-bridge/tools/kaggle_llm.py start
   python <...>/claude-kaggle-bridge/tools/kaggle_llm.py status   # ONLINE?
   ```
   (клієнтський конфіг шлюзу вже є: `~/.config/kagent/config.json`).
2. **Запустити міст** (тримати відкритим): `start_bridge.bat`. Локальний `ollama serve`
   не запускати — він забере порт 11434.
3. **Користуватись.** Вказати `OLLAMA_HOST=http://127.0.0.1:11434` і:
   ```
   ollama list
   ollama run qwen3-coder:30b "..."
   curl http://127.0.0.1:11434/api/generate -d "{\"model\":\"qwen3-coder:30b\",\"prompt\":\"hi\",\"stream\":false}"
   ```
   OpenAI-клієнти: `OPENAI_BASE_URL=http://127.0.0.1:11434/v1`, `OPENAI_API_KEY` будь-який.
4. **Завершити:** ядро саме вимикається після **10 хв простою** (економія квоти ~30 год/тижд);
   достроково — `kaggle_llm.py stop`. Поки ядра немає, міст на запити віддає `503` —
   просто підніми сесію знову (крок 1).

## Режими джерела (`config.json` -> `url_source_mode`)
- `kagent-gateway` (типово на bbeast) — читає `~/.config/kagent/config.json` (url+key+CA).
- `kaggle-llm` — прямий `endpoint.json` claude-kaggle-bridge (якщо мережа пускає trycloudflare).
- `pinned` / `file` / `registry` — для тесту/нестандарту.

## Статус — ПРАЦЮЄ (перевірено наживо 2026-10-13)
- [x] наскрізь: `ollama run qwen3-coder:30b` і `ollama list` через bbeast → VPS → Kaggle; `/api/generate`, `/v1/chat/completions` також
- [x] стрімінг наскрізь (`read1()`), підстановка токена, приватний CA шлюзу, BOM-стійкість, `HEAD /` як в Ollama
- [x] локальні перевірки проти контрольованого апстріму (401 без токена тощо)
