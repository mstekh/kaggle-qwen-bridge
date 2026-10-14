"""Офлайн-тест MCP-мосту bridge/qwen_mcp.py: підроблений OpenAI-сервер + справжній процес мосту по stdio.

Запуск:  python -m unittest discover -s tests -v
"""
import http.server
import json
import os
import subprocess
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
MCP = os.path.join(HERE, "..", "bridge", "qwen_mcp.py")
PORT, TOKEN = 18921, "test-token"
SEEN = []


class Fake(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._send(401, {"error": "unauthorized"})
        self._send(200, {"models": [{"name": "qwen3-coder:30b"}]})

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self._send(401, {"error": "unauthorized"})
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(req)
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": "echo: " + req["messages"][-1]["content"]}}]})


class Client:
    def __init__(self, env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("QWEN_")}
        e.update(env)
        self.p = subprocess.Popen([sys.executable, MCP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, env=e, encoding="utf-8")
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


class McpBridge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.c = Client({"QWEN_URL": "http://127.0.0.1:%d" % PORT, "QWEN_TOKEN": TOKEN})

    def tearDown(self):
        self.c.close()

    def test_handshake_and_tools(self):
        r = self.c.call("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(r["result"]["serverInfo"]["name"], "kaggle-qwen-bridge")
        names = [t["name"] for t in self.c.call("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["ask_qwen", "qwen_status"])

    def test_ask(self):
        r = self.c.call("tools/call", {"name": "ask_qwen", "arguments": {"prompt": "hi", "system": "be brief"}})
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(r["result"]["content"][0]["text"], "echo: hi")
        self.assertEqual(SEEN[-1]["messages"][0], {"role": "system", "content": "be brief"})

    def test_status(self):
        r = self.c.call("tools/call", {"name": "qwen_status", "arguments": {}})
        self.assertIn("qwen3-coder:30b", r["result"]["content"][0]["text"])

    def test_bad_prompt_is_tool_error(self):
        r = self.c.call("tools/call", {"name": "ask_qwen", "arguments": {"prompt": "  "}})
        self.assertTrue(r["result"]["isError"])

    def test_unknown_tool_and_method(self):
        self.assertIn("error", self.c.call("tools/call", {"name": "nope"}))
        self.assertIn("error", self.c.call("resources/list"))

    def test_wrong_token_is_tool_error_not_crash(self):
        c = Client({"QWEN_URL": "http://127.0.0.1:%d" % PORT, "QWEN_TOKEN": "bad"})
        try:
            r = c.call("tools/call", {"name": "ask_qwen", "arguments": {"prompt": "x"}})
            self.assertTrue(r["result"]["isError"])
            self.assertIn("401", r["result"]["content"][0]["text"])
        finally:
            c.close()

    def test_model_asleep_message(self):
        c = Client({"QWEN_URL": "http://127.0.0.1:9", "QWEN_TOKEN": "x"})   # порт без сервера
        try:
            r = c.call("tools/call", {"name": "qwen_status", "arguments": {}})
            self.assertTrue(r["result"]["isError"])
            self.assertIn("недоступна", r["result"]["content"][0]["text"])
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
