#!/usr/bin/env python3
"""connect.py — забирає адресу й токен моделі з журналу ядра Kaggle (лише стандартна бібліотека).

  python connect.py ВАШ-ЛОГІН/qwen-serve            знайти BRIDGE_ENDPOINT у живому журналі, перевірити, зберегти
  python connect.py --from-file output.log          розібрати журнал, завантажений через `kaggle kernels output`
  python connect.py ВАШ-ЛОГІН/qwen-serve --stop     вимкнути ядро (POST /__shutdown), щоб не палити квоту
  python connect.py --env                           надрукувати QWEN_URL / QWEN_TOKEN / QWEN_MODEL

Потрібен Kaggle CLI (змінна KAGGLE_BIN або `kaggle` у PATH). Адреса й токен пишуться в
~/.config/kaggle-qwen-bridge/endpoint.json (права лише власнику) і не потрапляють у репозиторій.
"""
import argparse
import json
import os
import pathlib
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

CONF = pathlib.Path.home() / ".config" / "kaggle-qwen-bridge"
EP_FILE = CONF / "endpoint.json"
EP_RE = re.compile(r"BRIDGE_ENDPOINT url=(https://[a-z0-9-]+\.trycloudflare\.com) token=([A-Za-z0-9_-]+) "
                   r"model=([^\s\"\\]+)")


def kaggle_bin():
    for c in (os.environ.get("KAGGLE_BIN"), shutil.which("kaggle")):
        if c and (os.path.exists(c) or shutil.which(c)):
            return c
    sys.exit("Kaggle CLI не знайдено: pip install kaggle (потрібен Python 3.11+) або задай KAGGLE_BIN")


def endpoint_from_text(text):
    hits = EP_RE.findall(text)
    return {"url": hits[-1][0], "token": hits[-1][1], "model": hits[-1][2]} if hits else None


def endpoint_from_live_log(kernel_id, window_s=40):
    """`kaggle kernels logs` без -f нічого не друкує для працюючого ядра, тому слухаємо живий потік."""
    p = subprocess.Popen([kaggle_bin(), "kernels", "logs", "-f", kernel_id], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, errors="replace")
    q = queue.Queue()
    threading.Thread(target=lambda: [q.put(l) for l in p.stdout] or q.put(None), daemon=True).start()
    seen, t0 = "", time.time()
    try:
        while time.time() - t0 < window_s:
            try:
                line = q.get(timeout=1)
            except queue.Empty:
                continue
            if line is None:
                break
            seen += line
            if EP_RE.search(seen) and time.time() - t0 > 4:
                break
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            p.kill()
    return endpoint_from_text(seen)


def request(ep, path, method="GET", timeout=25):
    req = urllib.request.Request(ep["url"] + path, method=method, data=b"" if method == "POST" else None,
                                 headers={"Authorization": "Bearer " + ep["token"]})
    return urllib.request.urlopen(req, timeout=timeout)


def check(ep):
    try:
        names = [m["name"] for m in json.loads(request(ep, "/api/tags").read()).get("models", [])]
        return True, names
    except Exception as e:
        return False, repr(e)


def save(ep):
    CONF.mkdir(parents=True, exist_ok=True)
    EP_FILE.write_text(json.dumps(ep), encoding="utf-8")
    if os.name == "nt":
        subprocess.run(["icacls", str(EP_FILE), "/inheritance:r", "/grant:r", "%s:F" % os.environ.get("USERNAME", "")],
                       capture_output=True)
    else:
        os.chmod(EP_FILE, 0o600)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kernel", nargs="?")
    ap.add_argument("--from-file")
    ap.add_argument("--env", action="store_true")
    ap.add_argument("--stop", action="store_true", help="вимкнути ядро за збереженою адресою")
    ap.add_argument("--no-check", action="store_true")
    a = ap.parse_args()
    if a.env or a.stop:
        ep = json.loads(EP_FILE.read_text(encoding="utf-8-sig"))
        if a.stop:
            print("відповідь:", request(ep, "/__shutdown", method="POST").read().decode())
            return
        print("QWEN_URL=%s\nQWEN_TOKEN=%s\nQWEN_MODEL=%s" % (ep["url"], ep["token"], ep["model"]))
        return
    if a.from_file:
        ep = endpoint_from_text(pathlib.Path(a.from_file).read_text(encoding="utf-8", errors="replace"))
    elif a.kernel:
        ep = endpoint_from_live_log(a.kernel)
    else:
        ap.error("потрібен ID ядра або --from-file")
    if not ep:
        sys.exit("рядка BRIDGE_ENDPOINT у журналі немає: ядро ще вантажить модель? Спробуй за хвилину.")
    print("знайдено: %s  model=%s" % (ep["url"], ep["model"]))
    if not a.no_check:
        ok, info = check(ep)
        print("перевірка /api/tags:", "OK, моделі: %s" % info if ok else "НЕ ВДАЛАСЬ: %s" % info)
    save(ep)
    print("збережено:", EP_FILE)


if __name__ == "__main__":
    main()
