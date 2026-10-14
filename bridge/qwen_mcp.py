#!/usr/bin/env python3
"""qwen_mcp.py — найпростіший міст: дає Claude (або будь-якому MCP-клієнту) інструмент `ask_qwen`,
який передає запит моделі Qwen на Kaggle.

MCP по stdio, лише стандартна бібліотека Python. Інструменти:
  ask_qwen(prompt, system?, max_tokens?, temperature?)  -> текст відповіді моделі
  qwen_status()                                          -> чи модель онлайн, яка саме

Адреса й токен беруться (кожного разу заново, бо адреса тунелю змінюється при кожному старті ядра):
  1. зі змінних QWEN_URL, QWEN_TOKEN, QWEN_MODEL;
  2. інакше з ~/.config/kaggle-qwen-bridge/endpoint.json (його пише bridge/connect.py).
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

ENDPOINT_FILE = pathlib.Path.home() / ".config" / "kaggle-qwen-bridge" / "endpoint.json"
PROTOCOL = "2025-06-18"
DEFAULT_TIMEOUT = 900

TOOLS = [
    {
        "name": "ask_qwen",
        "description": ("Ask the Qwen3-Coder-30B model running on a Kaggle GPU. Good for second opinions, "
                        "drafting code, summarising or explaining text. It has no tools and no memory: "
                        "put all needed context into `prompt`. Slow start: if the model is asleep, "
                        "answer is an error saying so."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The question or task, with all context."},
                "system": {"type": "string", "description": "Optional system prompt."},
                "max_tokens": {"type": "integer", "description": "Answer length limit (default 2048)."},
                "temperature": {"type": "number", "description": "0..1, default 0.2."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "qwen_status",
        "description": "Check whether the Kaggle Qwen model is reachable (online) and which model it is.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class BridgeError(Exception):
    pass


def load_endpoint():
    url, token, model = (os.environ.get("QWEN_URL"), os.environ.get("QWEN_TOKEN"), os.environ.get("QWEN_MODEL"))
    if not url:
        try:
            ep = json.loads(ENDPOINT_FILE.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise BridgeError("Немає адреси моделі: задай QWEN_URL/QWEN_TOKEN або запусти bridge/connect.py "
                              "(модель на Kaggle, мабуть, ще не запущена).")
        url, token, model = ep.get("url"), ep.get("token"), ep.get("model")
    if not url:
        raise BridgeError("У endpoint.json немає url.")
    return url.rstrip("/"), token or "", model or "qwen3-coder:30b"


def http_json(method, url, token, body=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise BridgeError("HTTP %s від моделі: %s" % (e.code, detail))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BridgeError("Модель недоступна (%s). Ядро на Kaggle спить або тунель змінився: "
                          "запусти ядро й повтори bridge/connect.py." % (getattr(e, "reason", e),))


def ask_qwen(args):
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BridgeError("`prompt` має бути непорожнім рядком.")
    url, token, model = load_endpoint()
    messages = []
    if isinstance(args.get("system"), str) and args["system"].strip():
        messages.append({"role": "system", "content": args["system"]})
    messages.append({"role": "user", "content": prompt})
    try:
        max_tokens = max(1, min(int(args.get("max_tokens", 2048)), 8192))
        temperature = max(0.0, min(float(args.get("temperature", 0.2)), 2.0))
    except (TypeError, ValueError):
        raise BridgeError("`max_tokens` має бути цілим числом, `temperature` — числом.")
    out = http_json("POST", url + "/v1/chat/completions", token,
                    {"model": model, "messages": messages, "stream": False,
                     "max_tokens": max_tokens, "temperature": temperature}, timeout=DEFAULT_TIMEOUT)
    try:
        return out["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise BridgeError("Несподівана відповідь моделі: %s" % json.dumps(out)[:300])


def qwen_status(_args):
    url, token, model = load_endpoint()
    tags = http_json("GET", url + "/api/tags", token, timeout=20)
    names = [m.get("name") for m in tags.get("models", [])]
    return "online: %s; моделі на сервері: %s" % (url.split("//", 1)[-1].split("/", 1)[0], ", ".join(names) or "—")


HANDLERS = {"ask_qwen": ask_qwen, "qwen_status": qwen_status}


def reply(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:            # notification: відповіді немає
        return
    if method == "initialize":
        proto = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        reply(msg_id, {"protocolVersion": proto, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "kaggle-qwen-bridge", "version": "0.1.0"}})
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params") or {}
        fn = HANDLERS.get(params.get("name"))
        if fn is None:
            return reply(msg_id, error={"code": -32602, "message": "unknown tool: %s" % params.get("name")})
        try:
            text, is_error = fn(params.get("arguments") or {}), False
        except BridgeError as e:
            text, is_error = str(e), True
        reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    else:
        reply(msg_id, error={"code": -32601, "message": "method not found: %s" % method})


def main():
    sys.stdin.reconfigure(encoding="utf-8")     # MCP — завжди UTF-8, незалежно від кодової сторінки Windows
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            handle(msg)


if __name__ == "__main__":
    main()
