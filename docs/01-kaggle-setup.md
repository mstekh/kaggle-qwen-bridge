# 1. Kaggle: акаунт, токен, CLI, квоти

## Акаунт

1. Зареєструйся на kaggle.com.
2. **Підтверди телефон** (Settings → Phone verification). Без цього в ядрах недоступні GPU та інтернет:
   ядро запуститься без відеокарти, і `ollama.com` не відкриється.

## Що дає безкоштовний акаунт (за досвідом автора, 2026-10)

| Ресурс | Значення |
|---|---|
| GPU | 2× Tesla T4 по 15 ГБ, або P100 |
| Квота GPU | ≈ 30 годин на тиждень |
| Одна сесія | до ~9 годин |
| Диск | ~20 ГБ у `/kaggle/working`; модель (18 ГБ) лягає в `/tmp` |
| Інтернет у ядрі | є, якщо верифіковано телефон |

Точні ліміти Kaggle змінює: дивись у профілі (Settings → Account) і на сторінці ядра.

## API-токен

Settings → API → Create New Token. Kaggle CLI 2.x читає токен із `~/.kaggle/access_token`
(Windows: `C:\Users\<ви>\.kaggle\access_token`); старий `kaggle.json` теж працює.
**Не клади токен у репозиторій.** Перевірити акаунт: `kaggle config view`.

## Kaggle CLI

```
pip install kaggle
```

CLI 2.x потребує Python 3.11+. На Windows `kaggle.exe` ставиться в `%APPDATA%\Python\PythonXY\Scripts`;
якщо його немає в PATH, задай повний шлях у змінній `KAGGLE_BIN`.

```
kaggle kernels push -p kernel --accelerator NvidiaTeslaT4   # створити/оновити ядро й запустити
kaggle kernels status ВАШ-ЛОГІН/qwen-serve                   # QUEUED, RUNNING, COMPLETE, ERROR
kaggle kernels logs -f ВАШ-ЛОГІН/qwen-serve                  # живий журнал (без -f для працюючого ядра порожньо)
kaggle kernels output ВАШ-ЛОГІН/qwen-serve -p out            # забрати журнал після завершення
```

## Чого CLI не вміє

Зупинити працююче ядро командою CLI не можна. Тому `serve.py` має власний вимикач:
`POST /__shutdown` із токеном (`python bridge/connect.py --stop`) і автовимкнення після `idle_min` хвилин простою.

## Підводні камені

- Нове ядро створюється приватним (`"is_private": true`). Лиши так: журнал містить токен доступу до моделі.
- Повторний `push`, поки ядро в черзі або працює, скасовує його й губить місце в черзі. Спершу `kernels status`.
- `title` у `kernel-metadata.json` має відповідати slug в `id`: `qwen-serve`.
- Налаштування моделі й контексту — словник `CONFIG` угорі `kernel/serve.py` (`model`, `ctx`, `idle_min`, `max_hours`).
  Для самоперевірки постав `selftest_exit: true`: ядро прожене тести, надрукує `SELFTEST …` і вимкнеться.
