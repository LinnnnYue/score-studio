#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Score Studio · 本地运行服务器（骨架验证用）
===========================================
无需 Node / Rust，单文件标准库实现：
  - 托管 src/ 下的前端（巴洛克无边框 UI 原型）
  - POST /api/process  → 调用 sheet_pipeline.run() 处理曲谱
  - GET  /api/library?dir=... → 列出输出目录中的 PDF

用法：
    python run_local.py [--port 8765] [--host 127.0.0.1]
然后在浏览器打开 http://127.0.0.1:8765/
（此形态使用浏览器窗口；真正的「无边框高级感」窗口由 Tauri 工程提供。）
"""

import argparse
import base64
import io
import json
import os
import re
import sys
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(HERE, "src")
UPLOAD_DIR = os.path.join(os.path.expanduser("~"), ".score-studio-uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
sys.path.insert(0, HERE)

import sheet_pipeline as sp  # noqa: E402
import library_ops as libops  # noqa: E402


def safe_upload_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '-', name).strip('-') or 'upload.bin'


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/library":
            q = urllib.parse.parse_qs(parsed.query)
            d = q.get("dir", [""])[0]
            try:
                items = sorted(
                    f for f in os.listdir(d) if f.lower().endswith(".pdf")
                ) if d and os.path.isdir(d) else []
            except Exception:
                items = []
            return self._send(200, {"items": items})
        if parsed.path == "/api/library_meta":
            q = urllib.parse.parse_qs(parsed.query)
            d = q.get("dir", [""])[0]
            try:
                return self._send(200, libops.get_library(d))
            except Exception as e:
                return self._send(200, [], ctype="application/json; charset=utf-8")
        if parsed.path == "/api/thumb":
            q = urllib.parse.parse_qs(parsed.query)
            d = q.get("dir", [""])[0]
            name = q.get("name", [""])[0]
            try:
                b64 = libops.get_thumb(d, name)
            except Exception:
                b64 = ''
            return self._send(200, b64, ctype="text/plain; charset=utf-8")
        if parsed.path == "/api/inspect":
            q = urllib.parse.parse_qs(parsed.query)
            d = q.get("dir", [""])[0]
            try:
                return self._send(200, libops.inspect_library(d))
            except Exception as e:
                return self._send(200, [], ctype="application/json; charset=utf-8")
        if parsed.path == "/api/wikitag":
            q = urllib.parse.parse_qs(parsed.query)
            title = q.get("title", [""])[0]
            artist = q.get("artist", [""])[0]
            try:
                return self._send(200, libops.wiki_tag(title, artist))
            except Exception:
                return self._send(200, None, ctype="application/json; charset=utf-8")
        if parsed.path == "/api/file":
            q = urllib.parse.parse_qs(parsed.query)
            p = q.get("path", [""])[0]
            full = os.path.normpath(os.path.abspath(p))
            if os.path.isfile(full):
                with open(full, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(data)))
                # RFC 5987：对中文文件名做 UTF-8 百分号编码，避免 latin-1 报错
                encoded_name = urllib.parse.quote(os.path.basename(full), safe='')
                self.send_header("Content-Disposition",
                                 f"inline; filename*=UTF-8''{encoded_name}")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return self.wfile.write(data)
            return self._send(404, {"error": "file not found"})
        # 静态文件（默认 index.html）
        rel = parsed.path.lstrip("/") or "index.html"
        # 防目录穿越
        full = os.path.normpath(os.path.join(SRC_DIR, rel))
        if not full.startswith(SRC_DIR) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"}, ctype="text/plain; charset=utf-8")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/upload":
            return self._handle_upload()
        if parsed.path == "/api/rename":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except Exception as e:
                return self._send(400, {"ok": False, "error": f"请求解析失败：{e}"})
            d = payload.get("dir", "")
            pairs = payload.get("pairs", [])
            return self._send(200, libops.rename_items(d, pairs))
        if parsed.path != "/api/process":
            return self._send(404, {"error": "not found"}, ctype="text/plain; charset=utf-8")
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            return self._send(400, {"ok": False, "error": f"请求解析失败：{e}"})

        inp = payload.get("input", "")
        out = payload.get("output_dir", r"G:\Lin_File\Documents\曲谱")
        theme = payload.get("theme", "")
        name = payload.get("name", "")

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            res_path = sp.run(inp, out, theme=theme, custom=name)
            ok = res_path is not None
        except Exception as e:  # noqa: BLE001
            sys.stdout = old
            log = buf.getvalue()
            return self._send(200, {"ok": False, "path": None, "log": log, "error": str(e)})
        sys.stdout = old
        log = buf.getvalue()
        return self._send(200, {"ok": ok, "path": res_path, "log": log, "error": None})

    def _handle_upload(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            filename = payload.get("filename", "upload.bin")
            data = payload.get("data", "")
        except Exception as e:
            return self._send(400, {"ok": False, "error": f"请求解析失败：{e}"})
        if "," in data:
            data = data.split(",", 1)[1]
        try:
            raw = base64.b64decode(data)
        except Exception as e:
            return self._send(400, {"ok": False, "error": f"base64 解码失败：{e}"})
        safe = safe_upload_name(filename)
        path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4().hex[:8]}_{safe}")
        with open(path, "wb") as f:
            f.write(raw)
        return self._send(200, {"ok": True, "path": path})


def main():
    ap = argparse.ArgumentParser(description="Score Studio 本地服务器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print("== Score Studio · 本地服务器 ==")
    print(f"  打开：{url}")
    print(f"  前端目录：{SRC_DIR}")
    print("  Ctrl+C 退出。")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
