"""Локальний тест шару «ремонту tool-calls» у kernel/serve.py без Kaggle і без GPU.

Піднімає підроблений Ollama, який відповідає виклик інструмента ТЕКСТОМ (як іноді робить Qwen-кодер),
ставить перед ним справжній проксі з serve.py і перевіряє, що клієнт отримує структуровані tool_calls
і у звичайному режимі, і у стрімінговому (SSE). Заодно перевіряє токен (401) і заборонений шлях (403).

Запуск:  python -m unittest discover -s tests -v
"""
import http.server
import importlib.util
import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("serve", os.path.join(HERE, "..", "kernel", "serve.py"))
serve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serve)

TEXT_CALL = "<function=get_weather>\n<parameter=city>\nKyiv\n</parameter>\n</function>"
UP_PORT, PROXY_PORT, TOKEN = 18911, 18912, "test-token"


def _chunk(proto, delta, reason=None):
    return {"id": "x", "object": "chat.completion.chunk", "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}


class FakeOllama(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"name": "m"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        if not req.get("stream"):
            body = json.dumps({"choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant",
                                                        "content": "Let me check. " + TEXT_CALL}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        pieces = ["Let me ", "check. ", "<func", "tion=get_weather>\n<param", "eter=city>\nKyiv\n</parameter>\n",
                  "</function>"]
        for p in pieces:
            data = b"data: " + json.dumps(_chunk(None, {"content": p})).encode() + b"\n\n"
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()
            time.sleep(0.05)
        for data in (b"data: " + json.dumps(_chunk(None, {}, "stop")).encode() + b"\n\n", b"data: [DONE]\n\n"):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.write(b"0\r\n\r\n")


class ToolCallRepair(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = http.server.ThreadingHTTPServer(("127.0.0.1", UP_PORT), FakeOllama)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cfg = dict(serve.CONFIG, proxy_port=PROXY_PORT, ollama_url="http://127.0.0.1:%d" % UP_PORT)
        cls.state = {"last": time.time(), "stop": False, "active": 0}
        cls.proxy = serve.start_proxy(cfg, TOKEN, cls.state)

    @classmethod
    def tearDownClass(cls):
        cls.proxy.shutdown()
        cls.up.shutdown()

    def _req(self, path, body=None, token=TOKEN, method=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        data = json.dumps(body).encode() if body is not None else None
        return urllib.request.Request("http://127.0.0.1:%d%s" % (PROXY_PORT, path), data=data,
                                      method=method or ("POST" if data else "GET"), headers=h)

    def test_parse_text_call(self):
        calls = serve.parse_text_tool_calls(TEXT_CALL)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "get_weather")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"city": "Kyiv"})

    def test_parse_json_form(self):
        calls = serve.parse_text_tool_calls('<tool_call>{"name": "f", "arguments": {"x": 1}}</tool_call>')
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"x": 1})

    def test_no_token_is_401(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self._req("/api/tags", token=None))
        self.assertEqual(cm.exception.code, 401)

    def test_forbidden_route_is_403(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self._req("/api/pull", {"name": "x"}))
        self.assertEqual(cm.exception.code, 403)

    def test_passthrough_tags(self):
        r = json.loads(urllib.request.urlopen(self._req("/api/tags")).read())
        self.assertEqual(r["models"][0]["name"], "m")

    def test_repair_non_stream(self):
        body = {"model": "m", "stream": False, "messages": [], "tools": [{"type": "function"}]}
        r = json.loads(urllib.request.urlopen(self._req("/v1/chat/completions", body)).read())
        ch = r["choices"][0]
        self.assertEqual(ch["finish_reason"], "tool_calls")
        self.assertEqual(ch["message"]["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertEqual(ch["message"]["content"], "Let me check.")

    def test_repair_stream(self):
        body = {"model": "m", "stream": True, "messages": [], "tools": [{"type": "function"}]}
        text, calls, reasons, done = "", [], [], False
        with urllib.request.urlopen(self._req("/v1/chat/completions", body)) as r:
            for line in r:
                line = line.strip()
                if line == b"data: [DONE]":
                    done = True
                elif line.startswith(b"data:"):
                    ch = json.loads(line[5:])["choices"][0]
                    d = ch.get("delta") or {}
                    text += d.get("content") or ""
                    calls += d.get("tool_calls") or []
                    if ch.get("finish_reason"):
                        reasons.append(ch["finish_reason"])
        self.assertTrue(done)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "get_weather")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"city": "Kyiv"})
        self.assertNotIn("<function", text)           # маркер не просочився в текст
        self.assertEqual(text.strip(), "Let me check.")
        self.assertEqual(reasons, ["tool_calls"])


if __name__ == "__main__":
    unittest.main()
