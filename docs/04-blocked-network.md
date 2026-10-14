# 4. Коли мережа блокує тунель Cloudflare

## Симптом

Ядро на Kaggle друкує `BRIDGE_ENDPOINT`, але з твого ПК адреса не відповідає: спершу `getaddrinfo failed`, далі таймаути.
`connect.py` покаже «перевірка НЕ ВДАЛАСЬ», а ядро за 10 хв засне без жодного запиту.

## Як відрізнити від проблем Kaggle

Іншим сайтам таймаутів немає, а IP Cloudflare для tunnel-доменів зависає:

```
curl -m 8 https://github.com
curl -m 8 --resolve test.trycloudflare.com:443:104.16.231.132 https://test.trycloudflare.com/
```

Автор бачив це на домашній мережі з роутером MikroTik; причину (роутер чи провайдер) не встановлював.
Швидка перевірка, чи справа в мережі: мобільний hotspot.

## Обхід

Не перевірялось у цьому репозиторії, лише описано як напрямок:

- запускати міст на машині в іншій мережі (VPS у датацентрі доходить до Cloudflare) і ходити до неї своїм каналом;
- замінити Cloudflare на інший тунель у `start_cloudflared()` в `kernel/serve.py` (ngrok, pinggy);
- VPN або Tailscale exit-node.
