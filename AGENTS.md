# AGENTS.md — kaggle-ollama-bridge

## Що це
Тонка надбудова над `Desktop/Projects/kaggle-projects/claude-kaggle-bridge`: дає локальний
Ollama/OpenAI endpoint `127.0.0.1:11434`, що проксіює на поточну Kaggle-сесію Qwen і сам
підставляє токен. Деталі — `README.md`. Стек: **Python stdlib only**, нуль pip-залежностей.

## Межі / правила
- **Нічого не ставити через pip** — тільки stdlib (свідоме обмеження, портативність).
- **Не дублювати Kaggle-бік.** Підняттям Qwen, тунелем і дискавері адреси займається
  `claude-kaggle-bridge` (`tools/kaggle_llm.py`, `endpoint.json`). Цей проєкт лише читає
  його `endpoint.json`. Не писати тут власне Kaggle-ядро / реєстр URL / тунель.
- `claude-kaggle-bridge` — **керований репозиторій** (single-writer lock, remote = джерело
  правди). Не редагувати його «мовчки». Якщо треба інший `SERVE_ID` (див. нижче) — робити
  через робочу копію або узгоджений env-оверрайд, не ламаючи його governance.
- Міст слухає лише `127.0.0.1` — назовні не виставляти. Токен Kaggle у логи не писати.

## Робочий шлях на bbeast = через kagent-gateway (НЕ прямий Kaggle)
- Мережа bbeast **блокує Cloudflare quick-tunnel** (TCP до tunnel-IP 104.16.x таймаутиться;
  github тощо — ОК). VPS досягає Cloudflare. Тому прямий режим `kaggle-llm` на bbeast не
  працює — ходимо через `kagent-gateway` на vps-nl (уже розгорнутий 2026-10-11, порт 8791,
  nginx :443 `/etc/nginx/conf.d/kagent.conf`). Деплою шлюзу я НЕ робив.
- bbeast під'єднаний як клієнт шлюзу: `~/.config/kagent/config.json` (url+key+CA),
  CA у `~/.config/kagent/ca.crt`. Додано через `kagent_gateway.py add-client bbeast`.
- Старт моделі: `python <ckb>/tools/kaggle_llm.py start` (gateway-режим авто, бо є config.json).

## Два акаунти Kaggle (пояснено)
- **`mykolasych`** — акаунт **шлюзу** на VPS (`/var/lib/kagent/.kaggle/access_token`); під нього
  й захардкоджений `SERVE_ID = mykolasych/claude-kaggle-serve` у dispatch.py — там усе узгоджено.
- **`micmikolus`** — локальний токен bbeast (`~/.kaggle/access_token`), CLI
  `~/AppData/Roaming/Python/Python312/Scripts/kaggle.exe`. Для ПРЯМОГО запуску з bbeast треба
  робоча копія з `SERVE_ID=micmikolus/...`, але прямий шлях усе одно блокує мережа — тож
  актуальний лише gateway-шлях (mykolasych).

## Локальний ollama CLI
- Локальний `ollama.exe` старий (0.33.1) проти сервера 0.40.3 — для `list`/`run` через міст
  вистачає. Локальний `ollama serve` НЕ запускати (забере порт 11434). CLI як клієнт:
  `OLLAMA_HOST=http://127.0.0.1:11434`.

## Перевірка (докази, не «виглядає добре»)
- Локально: фейковий апстрім, що вимагає Bearer-токен + фейковий `endpoint.json` →
  `/api/tags`, `/api/generate` (stream+non), `/v1/models`; контроль — прямий запит без
  токена = 401; стрім має йти чанками (не чекати повного тіла).
- Наскрізь: `kaggle-llm start` → `bridge.py` (режим `kaggle-llm`) → `curl /api/generate`
  до реального Qwen.

## Карта файлів
- `bridge.py` — `UrlResolver` (дискавері url+token з endpoint.json) + `ProxyHandler`
  (форвард, стрім через `read1()`, підстановка `Authorization`).
- `config.json`, `start_bridge.bat`.

## Changelog
- **2026-10-13** — створено й доведено до робочого стану. Спершу помилково заклав власне
  Kaggle-ядро + реєстр на VPS; знайшов наявний `claude-kaggle-bridge`, переробив у надбудову.
  Виявив, що мережа bbeast блокує Cloudflare-тунель → переключив на фронт наявного
  `kagent-gateway` (режим `kagent-gateway`, приватний CA, Bearer client-key). Виправлено:
  буферизацію стріму (`read1()`), BOM (`utf-8-sig`), підстановку токена, `HEAD /` (ollama CLI
  health-чекає сервер через HEAD / — без цього EOF), гасіння ConnectionReset у лог.
  **Наживо перевірено:** `ollama list`, `ollama run qwen3-coder:30b`, `/api/generate`,
  `/v1/chat/completions` — усе через bbeast → VPS → Kaggle. Видалено дублікати vps/ і kaggle/.
