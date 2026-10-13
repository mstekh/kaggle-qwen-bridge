#!/usr/bin/env python3
"""
bridge.py — локальний Ollama/OpenAI-сумісний reverse-proxy до Qwen на Kaggle.

Слухає 127.0.0.1:11434 (як справжній Ollama) і прозоро форвардить УСІ запити
(/api/* — Ollama, /v1/* — OpenAI) на поточний публічний URL Kaggle-тунелю.
Поточний URL береться з "реєстру" (спільне місце, куди Kaggle-ноутбук пише свою
адресу) з кешуванням і авто-оновленням, бо Kaggle-сесія ефемерна і URL щоразу новий.

Нуль сторонніх залежностей — лише stdlib. Стрімінг (NDJSON Ollama та SSE OpenAI)
проходить наскрізь без буферизації.

Запуск:  python bridge.py            (бере config.json поруч зі скриптом)
         python bridge.py other.json
"""
import http.server
import socketserver
import urllib.request
import urllib.error
import json
import os
import sys
import time
import threading
import ssl
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# Заголовки, які НЕ можна форвардити (hop-by-hop за RFC 7230 + Host/Content-Length,
# які http.client виставить сам під нове з'єднання).
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "accept-encoding",  # не хочемо стиснутого тіла — простіше стрімити as-is
}

DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",
    "listen_port": 11434,
    # Як дізнаватися поточний Kaggle-URL:
    #   "registry" — url_source це http(s) ендпоінт, що повертає {"url": "..."} або голий текст
    #   "file"     — url_source це локальний файл з URL у першому рядку
    #   "pinned"   — url_source це вже готовий цільовий URL (для тесту без реєстру)
    "url_source_mode": "pinned",
    "url_source": "http://127.0.0.1:9",   # заглушка; заповни реальним
    "refresh_seconds": 60,                 # як часто перечитувати реєстр у фоні
    "upstream_timeout": 600,               # таймаут на запит до Kaggle (ген. довга)
    "connect_timeout": 15,
    # ngrok free показує HTML-попередження на перший запит — цей заголовок його знімає
    "ngrok_skip_warning": True,
    # якщо реєстр/тунель захищені токеном — Bearer додається до апстрім-запиту
    "upstream_bearer": None,
    # Bearer для доступу до самого реєстру (якщо він закритий)
    "registry_bearer": None,
    "verify_tls": True,
    "log_file": "bridge.log",
}


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    if os.path.isfile(path):
        # utf-8-sig: терпимо до BOM, який додають редактори/PowerShell на Windows
        with open(path, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    return cfg


def _default_endpoint_json():
    return os.path.join(os.path.expanduser("~"), ".local", "state",
                        "kaggle-llm", "endpoint.json")


class UrlResolver:
    """Тримає поточний цільовий URL (+ токен) і вміє оновлювати його з джерела."""

    def __init__(self, cfg, logger):
        self.cfg = cfg
        self.log = logger
        self._url = None
        self._token = None
        self._cafile = None
        self._lock = threading.Lock()
        self._last_ok = 0.0

    @property
    def url(self):
        with self._lock:
            return self._url

    @property
    def token(self):
        with self._lock:
            return self._token

    @property
    def cafile(self):
        with self._lock:
            return self._cafile

    def _set(self, url, token=None, cafile=None):
        url = (url or "").strip().rstrip("/")
        if not url:
            return False
        with self._lock:
            changed = url != self._url
            self._url = url
            self._token = token or None
            self._cafile = cafile or None
            self._last_ok = time.time()
        if changed:
            self.log(f"target URL -> {url}" + ("  (token set)" if token else ""))
        return True

    def refresh(self):
        """Перечитати цільовий URL із джерела. Повертає True, якщо є дійсний URL."""
        mode = self.cfg["url_source_mode"]
        src = self.cfg["url_source"]
        try:
            if mode == "kagent-gateway":
                # Фронтимо наявний kagent-gateway на VPS (claude-kaggle-bridge/gateway):
                # його клієнтський конфіг дає url + ключ + приватний CA. Шлях через VPS
                # обходить блокування Cloudflare-тунелю в локальній мережі.
                gw = self.cfg.get("gateway_config") or os.path.join(
                    os.path.expanduser("~"), ".config", "kagent", "config.json")
                if not os.path.isfile(gw):
                    self.log(f"gateway config not found: {gw} "
                             f"(додай клієнта: kagent add-client)")
                    return False
                with open(gw, "r", encoding="utf-8-sig") as f:
                    d = json.load(f)
                return self._set(d.get("url", ""), d.get("key"), d.get("ca"))
            if mode == "kaggle-llm":
                # Переюзовуємо наявний claude-kaggle-bridge: його `kaggle-llm start`
                # кладе сюди {url, token, model, ...} поточної Kaggle-сесії.
                ep = self.cfg.get("endpoint_json") or _default_endpoint_json()
                if not os.path.isfile(ep):
                    self.log(f"endpoint.json not found: {ep} "
                             f"(підніми сесію: kaggle-llm start)")
                    return False
                with open(ep, "r", encoding="utf-8-sig") as f:
                    d = json.load(f)
                return self._set(d.get("url", ""), d.get("token"))
            if mode == "pinned":
                return self._set(src)
            if mode == "file":
                if not os.path.isfile(src):
                    self.log(f"url file not found: {src}")
                    return False
                with open(src, "r", encoding="utf-8-sig") as f:
                    return self._set(f.readline())
            if mode == "registry":
                req = urllib.request.Request(src, method="GET")
                b = self.cfg.get("registry_bearer")
                if b:
                    req.add_header("Authorization", f"Bearer {b}")
                req.add_header("ngrok-skip-browser-warning", "true")
                ctx = None
                if src.startswith("https") and not self.cfg.get("verify_tls", True):
                    ctx = ssl._create_unverified_context()
                with urllib.request.urlopen(
                    req, timeout=self.cfg["connect_timeout"], context=ctx
                ) as r:
                    raw = r.read().decode("utf-8", "replace").strip()
                try:
                    return self._set(json.loads(raw).get("url", ""))
                except (ValueError, AttributeError):
                    return self._set(raw)  # реєстр віддав голий текст
            self.log(f"unknown url_source_mode: {mode}")
            return False
        except Exception as e:  # мережа/парсинг — не валимо проксі
            self.log(f"url refresh failed: {e}")
            return False

    def run_background(self):
        def loop():
            while True:
                self.refresh()
                time.sleep(max(5, int(self.cfg["refresh_seconds"])))
        t = threading.Thread(target=loop, daemon=True)
        t.start()


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg = None          # інжектиться у main()
    resolver = None
    log_fn = None

    # Прибираємо стандартний галасливий лог BaseHTTPRequestHandler
    def log_message(self, fmt, *args):
        pass

    def handle(self):
        # Клієнти (ollama) часто різко рвуть keep-alive після відповіді — це норма,
        # не засмічуємо лог трейсбеками ConnectionReset/BrokenPipe.
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    # --- health/-корінь: справжній Ollama на GET / віддає "Ollama is running" ---
    def _maybe_local(self):
        # Ollama-клієнт health-чекає сервер через HEAD / (і GET /) — відповідаємо локально
        # 200, як справжній Ollama; інакше запит піде на шлюз, де location / = 404.
        if self.command in ("GET", "HEAD") and self.path in ("/", ""):
            body = b"Ollama is running"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command == "GET":
                self.wfile.write(body)
            return True
        if self.path == "/__bridge/health":
            url = self.resolver.url
            body = json.dumps({
                "bridge": "kaggle-ollama-bridge",
                "target": url,
                "healthy": bool(url),
                "ts": datetime.now(timezone.utc).isoformat(),
            }).encode()
            self.send_response(200 if url else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        if self.command == "GET" and self.path == "/__bridge/status":
            body = json.dumps(self._bridge_status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        if self.command in ("GET", "HEAD") and self.path.rstrip("/") == "/ui":
            path = os.path.join(HERE, "ui.html")
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                body = b"<h1>ui.html not found</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command == "GET":
                self.wfile.write(body)
            return True
        return False

    def _ctx_for(self, target):
        """SSL-контекст для апстріму: приватний CA шлюзу або вимкнена перевірка."""
        if not target.startswith("https"):
            return None
        if not self.cfg.get("verify_tls", True):
            return ssl._create_unverified_context()
        cafile = self.resolver.cafile or self.cfg.get("upstream_cafile")
        if cafile:
            return ssl.create_default_context(cafile=cafile)
        return None

    def _bridge_status(self):
        """Жива перевірка всього ланцюга для веб-UI: міст -> шлюз -> ядро Kaggle."""
        target = self.resolver.url
        out = {"bridge": True, "target": target, "kernel": "unknown",
               "model": None, "detail": None}
        if not target:
            out["kernel"] = "no-target"
            out["detail"] = "немає цільового URL (перевір config.json)"
            return out
        try:
            req = urllib.request.Request(target + "/api/tags", method="GET")
            token = self.resolver.token or self.cfg.get("upstream_bearer")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=8,
                                        context=self._ctx_for(target)) as r:
                data = json.loads(r.read())
            models = [m.get("name") for m in data.get("models", [])]
            out["kernel"] = "online" if models else "online-empty"
            out["model"] = models[0] if models else None
        except urllib.error.HTTPError as e:
            if e.code == 503:
                out["kernel"] = "offline"
                out["detail"] = "ядро Kaggle спить — підніми: kaggle_llm.py start"
            else:
                out["kernel"] = "error"
                out["detail"] = f"шлюз відповів {e.code}"
        except Exception as e:
            out["kernel"] = "unreachable"
            out["detail"] = f"шлюз/інтернет недоступні: {e}"
        return out

    def _read_body(self):
        length = self.headers.get("Content-Length")
        if length is not None:
            return self.rfile.read(int(length))
        if (self.headers.get("Transfer-Encoding", "").lower() == "chunked"):
            # малоймовірно для цих клієнтів; зчитаємо просто чанки
            data = b""
            while True:
                size_line = self.rfile.readline().strip()
                if not size_line:
                    break
                size = int(size_line, 16)
                if size == 0:
                    self.rfile.readline()
                    break
                data += self.rfile.read(size)
                self.rfile.readline()
            return data
        return b""

    def _build_upstream(self, target, body):
        url = target + self.path
        req = urllib.request.Request(url, data=body if body else None,
                                     method=self.command)
        # Токен ендпоінта (з endpoint.json) має пріоритет над тим, що прислав клієнт:
        # локальні інструменти Kaggle-токена не знають, а Kaggle-проксі його вимагає.
        token = self.resolver.token or self.cfg.get("upstream_bearer")
        for k, v in self.headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            if k.lower() == "authorization" and token:
                continue  # замінимо своїм нижче
            req.add_header(k, v)
        if self.cfg.get("ngrok_skip_warning", True):
            req.add_header("ngrok-skip-browser-warning", "true")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        return req

    def _proxy(self):
        if self._maybe_local():
            return
        body = self._read_body()
        target = self.resolver.url
        if not target:
            # реєстр ще не дав URL — спробуємо разово оновити
            self.resolver.refresh()
            target = self.resolver.url
        if not target:
            return self._error(503, "no Kaggle target URL yet (registry empty)")

        ctx = self._ctx_for(target)

        attempts = 0
        while attempts < 2:
            attempts += 1
            req = self._build_upstream(target, body)
            try:
                resp = urllib.request.urlopen(
                    req, timeout=self.cfg["upstream_timeout"], context=ctx)
            except urllib.error.HTTPError as e:
                # апстрім відповів помилкою — віддаємо її як є
                return self._relay_response(e)
            except Exception as e:
                self.log_fn(f"upstream error ({target}): {e}")
                # URL міг протухнути — оновимо реєстр і спробуємо ще раз
                if attempts < 2 and self.resolver.refresh():
                    new = self.resolver.url
                    if new and new != target:
                        target = new
                        continue
                return self._error(502, f"upstream unreachable: {e}")
            return self._relay_response(resp)

    def _relay_response(self, resp):
        """Стрімимо відповідь апстріму назад клієнту без буферизації."""
        status = getattr(resp, "status", None) or resp.getcode()
        if self.cfg.get("log_requests"):
            ua = self.headers.get("User-Agent", "-")
            self.log_fn(f"{self.command} {self.path} -> {status} (ua={ua})")
        self.send_response(status)
        headers = resp.headers
        # Чи буде стрім (NDJSON/SSE)? Тоді віддаємо chunked.
        clen = headers.get("Content-Length")
        for k, v in headers.items():
            if k.lower() in HOP_BY_HOP or k.lower() == "content-length":
                continue
            self.send_header(k, v)
        chunked = clen is None
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", clen)
        self.end_headers()
        # read1(): віддає байти, щойно вони прийшли від апстріму, не чекаючи заповнення
        # буфера — інакше http.client на chunked-відповіді накопичує і стрім ламається.
        reader = resp.read1 if hasattr(resp, "read1") else resp.read
        try:
            while True:
                chunk = reader(65536)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # клієнт відвалився — норм
        finally:
            try:
                resp.close()
            except Exception:
                pass

    def _error(self, code, msg):
        body = json.dumps({"error": msg}).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # Усі методи йдуть в один обробник
    do_GET = _proxy
    do_POST = _proxy
    do_PUT = _proxy
    do_DELETE = _proxy
    do_HEAD = _proxy
    do_PATCH = _proxy


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_logger(cfg):
    path = os.path.join(HERE, cfg["log_file"])
    lock = threading.Lock()

    def log(msg):
        line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
        with lock:
            print(line, flush=True)
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
    return log


def main():
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "config.json")
    cfg = load_config(cfg_path)
    log = make_logger(cfg)
    log(f"config: {cfg_path}  mode={cfg['url_source_mode']}  src={cfg['url_source']}")

    resolver = UrlResolver(cfg, log)
    resolver.refresh()
    resolver.run_background()

    ProxyHandler.cfg = cfg
    ProxyHandler.resolver = resolver
    ProxyHandler.log_fn = log

    srv = ThreadingHTTPServer((cfg["listen_host"], cfg["listen_port"]), ProxyHandler)
    log(f"listening on http://{cfg['listen_host']}:{cfg['listen_port']}  "
        f"(Ollama + OpenAI API -> Kaggle Qwen)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        srv.shutdown()


if __name__ == "__main__":
    main()
