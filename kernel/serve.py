#!/usr/bin/env python3
"""serve.py — Ollama + кодерська модель на GPU Kaggle, відкрита назовні через Cloudflare quick tunnel.

Що робить (у такому порядку):
  1. ставить Ollama і піднімає `ollama serve`;
  2. тягне модель (CONFIG["model"]) і «прогріває» її;
  3. піднімає невеликий проксі з перевіркою випадкового токена (його генерує саме ядро);
  4. відкриває проксі назовні через `cloudflared` (адреса *.trycloudflare.com, без акаунта);
  5. друкує у ПРИВАТНИЙ журнал ядра рядок  BRIDGE_ENDPOINT url=... token=... model=...;
  6. (за потреби) проганяє самотест і потім тримає модель, доки є запити.

Ядро саме вимикається після CONFIG["idle_min"] хвилин без запитів або через CONFIG["max_hours"].
Лише стандартна бібліотека Python. Файл виконується на Kaggle; локально його можна
імпортувати для тестів (див. tests/test_toolcall_repair.py) — main() запускається тільки як скрипт.

Шар «ремонту tool-calls» нижче потрібен клієнтам, які викликають інструменти через OpenAI-сумісний API:
Qwen-кодер іноді друкує виклик текстом, і без ремонту клієнт його не побачить.
"""
import hmac
import http.server
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request

# ───────────────────────────── НАЛАШТУВАННЯ — редагуй тут ─────────────────────────────
CONFIG = {
    "model": "qwen3-coder:30b",            # тег Ollama; 30B (Q4, ~18 ГБ) влазить на 2× T4
    "fallback_model": "qwen2.5-coder:14b",  # якщо основну не вдалося стягнути
    "ctx": 32768,                           # контекст; 65536 теж влазить на 2× T4
    "idle_min": 10,                         # вимкнутись після стількох хвилин без запитів
    "max_hours": 8,                         # жорстка межа часу сесії
    "selftest": True,                       # прогнати перевірки після старту і надрукувати SELFTEST ...
    "selftest_exit": False,                 # true = після самотесту одразу вимкнутись (для перевірки)
    "proxy_port": 8080,
    "ollama_url": "http://127.0.0.1:11434",
}
# ───────────────────────────────────────────────────────────────────────────────────────

# Шляхи, які проксі пропускає; решта (/api/pull, /api/delete, /api/create …) — 403.
ALLOWED_ROUTES = {
    ("GET", "/api/tags"), ("GET", "/api/version"), ("GET", "/api/ps"),
    ("POST", "/api/chat"), ("POST", "/api/generate"), ("POST", "/api/show"),
    ("POST", "/api/embed"), ("POST", "/api/embeddings"),
    ("POST", "/v1/chat/completions"), ("POST", "/v1/completions"), ("POST", "/v1/embeddings"),
    ("GET", "/v1/models"),
}
MAX_BODY = 32 * 1024 * 1024

# Qwen-кодери іноді друкують виклик інструмента ТЕКСТОМ (після пояснень або без відкривального
# <tool_call>); Ollama тоді віддає його як звичайний текст, а клієнти його
# ігнорують. Проксі повертає такий текст у структуровані OpenAI tool_calls.
TOOL_MARKERS = ("<function=", "<tool_call>")
FUNC_RE = re.compile(r"<function=([^>\s]+)>(.*?)(?:</function>|$)", re.S)
PARAM_RE = re.compile(r"<parameter=([^>\s]+)>(.*?)(?:</parameter>|(?=<parameter=)|$)", re.S)
TC_JSON_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|$)", re.S)


def _param_value(v):
    v = v.strip("\n")
    s = v.strip()
    if s[:1] in "[{" or s in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", s or "x"):
        try:
            return json.loads(s)
        except ValueError:
            pass
    return v


def parse_text_tool_calls(text):
    """Текстові виклики інструментів (Qwen XML або <tool_call>{json}) -> список OpenAI tool_calls; [] якщо немає."""
    calls = []
    for m in FUNC_RE.finditer(text):
        args = {p.group(1): _param_value(p.group(2)) for p in PARAM_RE.finditer(m.group(2))}
        calls.append((m.group(1), args))
    if not calls:
        for m in TC_JSON_RE.finditer(text):
            try:
                j = json.loads(m.group(1))
            except ValueError:
                continue
            args = j.get("arguments", j.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"input": args}
            if j.get("name"):
                calls.append((j["name"], args))
    return [{"index": i, "id": "call_kb%s" % secrets.token_hex(4), "type": "function",
             "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
            for i, (name, args) in enumerate(calls)]


def split_at_marker(text):
    """-> (безпечно_віддати, притримати, маркер_знайдено). Притримує хвіст, що може бути початком маркера."""
    idx = [text.find(mk) for mk in TOOL_MARKERS if mk in text]
    if idx:
        k = min(idx)
        return text[:k], text[k:], True
    hold = 0
    for mk in TOOL_MARKERS:
        for n in range(min(len(mk) - 1, len(text)), 0, -1):
            if text.endswith(mk[:n]):
                hold = max(hold, n)
                break
    return (text[:-hold], text[-hold:], False) if hold else (text, "", False)


def repair_completion(obj):
    """Відповідь /v1/chat/completions без стрімінгу: текстові tool-calls -> message.tool_calls."""
    try:
        ch = obj["choices"][0]
        msg = ch["message"]
    except (KeyError, IndexError, TypeError):
        return obj, 0
    content = msg.get("content") or ""
    if msg.get("tool_calls") or not any(mk in content for mk in TOOL_MARKERS):
        return obj, 0
    before, rest, _ = split_at_marker(content)
    calls = parse_text_tool_calls(rest)
    if not calls:
        return obj, 0
    for c in calls:
        c.pop("index", None)
    msg["content"] = before.strip() or None
    msg["tool_calls"] = calls
    ch["finish_reason"] = "tool_calls"
    return obj, len(calls)


def sh(cmd, timeout=600, check=False):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError("command failed (%s): %s\n%s" % (p.returncode, cmd, (p.stdout + p.stderr)[-1500:]))
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def log(*a):
    print("[serve %s]" % time.strftime("%H:%M:%S"), *a, flush=True)


def install_ollama():
    rc, _ = sh("command -v ollama")
    if rc == 0:
        return
    sh("apt-get update -qq && apt-get install -y -qq zstd", timeout=300)
    rc, out = sh("curl -fsSL https://ollama.com/download/ollama-linux-amd64.tar.zst | tar x -C /usr --zstd", timeout=900)
    log("ollama tarball install rc=%s %s" % (rc, out[-300:].strip()))
    if sh("command -v ollama")[0] != 0:
        rc, out = sh("curl -fsSL https://ollama.com/install.sh | sh", timeout=900)
        log("ollama install.sh rc=%s %s" % (rc, out[-300:].strip()))
    rc, out = sh("ollama --version")
    log("ollama:", out.strip()[:200])


def start_ollama(cfg):
    env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434", OLLAMA_MODELS="/tmp/ollama-models",
               OLLAMA_FLASH_ATTENTION="1", OLLAMA_KEEP_ALIVE="2h", OLLAMA_MAX_LOADED_MODELS="1",
               OLLAMA_NUM_PARALLEL="1", OLLAMA_CONTEXT_LENGTH=str(cfg["ctx"]))
    logf = open("/tmp/ollama-serve.log", "w")
    proc = subprocess.Popen(["ollama", "serve"], env=env, stdout=logf, stderr=subprocess.STDOUT)
    for _ in range(90):
        try:
            urllib.request.urlopen(cfg["ollama_url"] + "/api/tags", timeout=3).read()
            log("ollama api is up")
            return proc
        except Exception:
            time.sleep(2)
    raise RuntimeError("ollama did not start: " + open("/tmp/ollama-serve.log").read()[-1500:])


def pull(model):
    t0 = time.time()
    p = subprocess.run(["ollama", "pull", model], capture_output=True, text=True, timeout=3600)
    ok = p.returncode == 0
    last = ((p.stdout or "") + (p.stderr or "")).replace("\r", "\n").strip().split("\n")[-1][:200]
    log("pull %s ok=%s in %.0fs: %s" % (model, ok, time.time() - t0, last))
    return ok


def warm_up(cfg, model):
    body = json.dumps({"model": model, "stream": False,
                       "messages": [{"role": "user", "content": "Reply with the single word OK."}],
                       "options": {"num_ctx": cfg["ctx"], "num_predict": 8}}).encode()
    req = urllib.request.Request(cfg["ollama_url"] + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    urllib.request.urlopen(req, timeout=1800).read()
    log("model %s loaded, warm-up %.1fs" % (model, time.time() - t0))


def start_proxy(cfg, token, state):
    """Проксі перед Ollama: перевіряє токен, пускає лише дозволені шляхи, стрімить відповіді (NDJSON / SSE)."""
    upstream = cfg["ollama_url"]
    lock = threading.Lock()
    state.setdefault("active", 0)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _plain(self, code, text):
            b = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _go(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return self._plain(401, "unauthorized")
            path = self.path.split("?", 1)[0]
            if path == "/__shutdown":
                if self.command != "POST":
                    return self._plain(405, "POST only")
                state["stop"] = True
                return self._plain(200, "stopping")
            if (self.command, path) not in ALLOWED_ROUTES:
                return self._plain(403, "route not allowed: %s %s" % (self.command, path))
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                self.close_connection = True
                return self._plain(411, "send a Content-Length body, chunked request bodies are not supported")
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._plain(400, "bad Content-Length")
            if n < 0 or n > MAX_BODY:
                return self._plain(413, "body too large")
            with lock:
                state["active"] += 1
                state["last"] = time.time()
            try:
                body = self.rfile.read(n) if n else None
                if path == "/v1/chat/completions" and body:
                    try:
                        reqj = json.loads(body)
                    except ValueError:
                        reqj = {}
                    if reqj.get("tools"):
                        return self._tools_completion(body, bool(reqj.get("stream")))
                headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
                if self.headers.get("Accept"):
                    headers["Accept"] = self.headers["Accept"]
                req = urllib.request.Request(upstream + self.path, data=body, method=self.command, headers=headers)
                try:
                    resp = urllib.request.urlopen(req, timeout=3600)
                    code = resp.status
                except urllib.error.HTTPError as e:
                    resp, code = e, e.code
                except Exception as e:
                    return self._plain(502, "upstream error: %r" % (e,))
                self.send_response(code)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                read = getattr(resp, "read1", resp.read)  # read1: віддавати байти одразу, не чекаючи повного буфера
                while True:
                    chunk = read(65536)
                    if not chunk:
                        break
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                    state["last"] = time.time()
                self.wfile.write(b"0\r\n\r\n")
            finally:
                with lock:
                    state["active"] -= 1
                    state["last"] = time.time()

        def _chunk(self, data):
            if data:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

        def _tools_completion(self, body, stream):
            """/v1/chat/completions з tools: пропускаємо, але текстові tool-calls перетворюємо на tool_calls."""
            req = urllib.request.Request(upstream + "/v1/chat/completions", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                resp = urllib.request.urlopen(req, timeout=3600)
            except urllib.error.HTTPError as e:
                return self._plain(e.code, e.read().decode("utf-8", "replace")[:2000])
            except Exception as e:
                return self._plain(502, "upstream error: %r" % (e,))
            if not stream:
                obj, n = repair_completion(json.loads(resp.read()))
                if n:
                    state["repaired"] = state.get("repaired", 0) + n
                    log("repaired %d text tool call(s) (non-stream)" % n)
                out = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            pending, buf, buffering, saw_tool_calls, last = "", "", False, False, None
            raw = b""
            read = getattr(resp, "read1", resp.read)

            def emit(obj):
                self._chunk(b"data: " + json.dumps(obj).encode() + b"\n\n")

            def content_chunk(proto, text):
                o = json.loads(json.dumps(proto))
                o["choices"][0]["delta"] = {"content": text}
                o["choices"][0]["finish_reason"] = None
                return o

            def finish(proto, reason):
                nonlocal pending, buf
                if buffering:
                    calls = parse_text_tool_calls(buf)
                    if calls:
                        state["repaired"] = state.get("repaired", 0) + len(calls)
                        log("repaired %d text tool call(s) (stream)" % len(calls))
                        o = json.loads(json.dumps(proto))
                        o["choices"][0]["delta"] = {"tool_calls": calls}
                        o["choices"][0]["finish_reason"] = None
                        emit(o)
                        reason = "tool_calls"
                    else:
                        emit(content_chunk(proto, buf))
                elif pending:
                    emit(content_chunk(proto, pending))
                pending, buf = "", ""
                o = json.loads(json.dumps(proto))
                o["choices"][0]["delta"] = {}
                o["choices"][0]["finish_reason"] = reason
                emit(o)

            while True:
                data = read(65536)
                if not data:
                    break
                state["last"] = time.time()
                raw += data
                while b"\n" in raw:
                    line, raw = raw.split(b"\n", 1)
                    s = line.strip()
                    if not s:
                        continue
                    if s == b"data: [DONE]":
                        if last is not None and not saw_tool_calls and (pending or buf):
                            finish(last, "stop")
                        self._chunk(b"data: [DONE]\n\n")
                        continue
                    if not s.startswith(b"data:"):
                        self._chunk(s + b"\n\n")
                        continue
                    try:
                        obj = json.loads(s[5:])
                        ch = obj["choices"][0]
                    except (ValueError, KeyError, IndexError):
                        self._chunk(s + b"\n\n")
                        continue
                    last = obj
                    delta = ch.get("delta") or {}
                    if delta.get("tool_calls"):
                        saw_tool_calls = True
                        if pending:
                            emit(content_chunk(obj, pending))
                            pending = ""
                        self._chunk(s + b"\n\n")
                        continue
                    text = delta.get("content") or ""
                    reason = ch.get("finish_reason")
                    if buffering:
                        buf += text
                    elif text:
                        safe, held, found = split_at_marker(pending + text)
                        if safe:
                            emit(content_chunk(obj, safe))
                        if found:
                            buffering, buf, pending = True, held, ""
                        else:
                            pending = held
                    if reason:
                        if saw_tool_calls:
                            self._chunk(s + b"\n\n")
                        else:
                            finish(obj, reason)
                        pending, buf = "", ""
            self.wfile.write(b"0\r\n\r\n")

        do_GET = do_POST = do_PUT = do_DELETE = _go

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", cfg["proxy_port"]), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_cloudflared(cfg):
    exe = "/usr/local/bin/cloudflared"
    if not os.path.exists(exe):
        sh("curl -fsSL -o %s https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
           " && chmod +x %s" % (exe, exe), timeout=300, check=True)
    logf = "/tmp/cloudflared.log"
    p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url",
                          "http://127.0.0.1:%d" % cfg["proxy_port"]], stdout=open(logf, "w"), stderr=subprocess.STDOUT)
    for _ in range(90):
        time.sleep(2)
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open(logf, errors="replace").read())
        if m:
            return p, m.group(0)
        if p.poll() is not None:
            break
    raise RuntimeError("cloudflared failed: " + open(logf, errors="replace").read()[-1500:])


def selftest(cfg, token, model, tunnel_url):
    """Перевірки зсередини ядра. Результат — рядки `SELFTEST <назва> OK|FAIL` у журналі. Повертає True, якщо все OK."""
    base = "http://127.0.0.1:%d" % cfg["proxy_port"]
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        log("SELFTEST %-36s %s %s" % (name, "OK  " if ok else "FAIL", str(detail)[:160]))

    def call(method, path, body=None, auth=True, timeout=600, root=base):
        h = {"Content-Type": "application/json"}
        if auth:
            h["Authorization"] = "Bearer " + token
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(root + path, data=data, method=method, headers=h)
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:
            return 0, repr(e).encode()

    code, _ = call("GET", "/api/tags", auth=False)
    check("без токена -> 401", code == 401, code)
    code, b = call("GET", "/api/tags")
    names = [m.get("name") for m in json.loads(b).get("models", [])] if code == 200 else []
    check("з токеном /api/tags", code == 200 and model in names, names)
    code, _ = call("POST", "/api/pull", {"name": "x"})
    check("заборонений шлях -> 403", code == 403, code)

    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "Get the current weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
    msgs = [{"role": "user", "content": "What is the weather in Kyiv? Call the get_weather tool."}]
    code, b = call("POST", "/v1/chat/completions", {"model": model, "messages": msgs, "tools": tools, "stream": False})
    calls = []
    if code == 200:
        calls = json.loads(b)["choices"][0]["message"].get("tool_calls") or []
    check("tool-call (без стріму)", code == 200 and bool(calls), calls[0]["function"] if calls else code)

    req = urllib.request.Request(base + "/v1/chat/completions", method="POST",
                                 data=json.dumps({"model": model, "messages": msgs, "tools": tools,
                                                  "stream": True}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    saw_tc, saw_done, n_chunks = False, False, 0
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for line in r:
                line = line.strip()
                if line == b"data: [DONE]":
                    saw_done = True
                elif line.startswith(b"data:"):
                    n_chunks += 1
                    try:
                        if (json.loads(line[5:])["choices"][0].get("delta") or {}).get("tool_calls"):
                            saw_tc = True
                    except (ValueError, KeyError, IndexError):
                        pass
    except Exception as e:
        log("stream test error: %r" % (e,))
    check("tool-call (стрім SSE)", saw_tc and saw_done, "chunks=%d" % n_chunks)

    if tunnel_url:
        code, b = 0, b""
        for _ in range(12):  # DNS нового тунелю може з'явитись із затримкою
            code, b = call("GET", "/api/tags", root=tunnel_url, timeout=20)
            if code == 200:
                break
            time.sleep(5)
        check("тунель зсередини Kaggle", code == 200, code)
    log("SELFTEST SUMMARY %d/%d OK" % (sum(results), len(results)))
    return all(results)


def main():
    cfg = CONFIG
    t0 = time.time()
    log(sh("nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader")[1].strip())
    install_ollama()
    ollama_proc = start_ollama(cfg)
    model = cfg["model"]
    if not pull(model):
        model = cfg["fallback_model"]
        if not pull(model):
            raise RuntimeError("no model could be pulled")
    warm_up(cfg, model)
    token = secrets.token_urlsafe(24)
    state = {"last": time.time(), "stop": False, "active": 0, "model": model}
    start_proxy(cfg, token, state)
    cf, url = start_cloudflared(cfg)
    announce = "BRIDGE_ENDPOINT url=%s token=%s model=%s"
    print(announce % (url, token, model), flush=True)
    log("serving; idle shutdown after %s min, hard stop after %s h, ctx %s" % (
        cfg["idle_min"], cfg["max_hours"], cfg["ctx"]))
    if cfg["selftest"]:
        ok = selftest(cfg, token, model, url)
        state["last"] = time.time()
        if cfg["selftest_exit"]:
            log("selftest_exit=true: %s, shutting down" % ("all OK" if ok else "FAILURES"))
            cf.terminate()
            ollama_proc.terminate()
            return
    deadline = t0 + cfg["max_hours"] * 3600
    while True:
        time.sleep(20)
        if state["stop"]:
            log("shutdown requested")
            break
        idle = time.time() - state["last"]
        if state["active"] == 0 and idle > cfg["idle_min"] * 60:
            log("idle for %.0f min, shutting down" % (idle / 60))
            break
        if time.time() > deadline:
            log("max serve time reached")
            break
        if cf.poll() is not None:
            log("cloudflared exited, restarting")
            cf, url = start_cloudflared(cfg)
            print(announce % (url, token, model), flush=True)
    cf.terminate()
    ollama_proc.terminate()
    log("served for %.0f min, repaired tool calls: %d" % ((time.time() - t0) / 60, state.get("repaired", 0)))


if __name__ == "__main__":
    main()
