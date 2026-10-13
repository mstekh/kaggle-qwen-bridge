@echo off
REM Запуск локального моста. Якщо встановлений справжній Ollama — спершу зупини його,
REM бо він займе порт 11434: (net stop / taskkill ollama).
cd /d "%~dp0"
python bridge.py %*
