#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kimi schema-fix middleware（CC Switch -> Kimi For Coding 专用）

背景：Codex 更新后，动态工具生成的参数 schema 是 JSON Schema 2020-12 风格，
$defs 里出现 $ref 与兄弟关键字（type/format/minLength 等）共存的写法。
OpenAI 官方 API 接受，但 Kimi 的 walle 校验器按 draft-07/OpenAPI 3.0 规则
拒绝（HTTP 400: "tools.function.parameters is not a valid moonshot flavored
json schema"）。cc-switch 原样转发，不做规范化。

本代理做的事（等价于 cc-switch PR #6627 的 inline_ref_siblings）：
  对每个请求体里的 tools[].function.parameters 做全量 $ref 解引用
  （把被引用的 $defs 内容内联合并，兄弟关键字优先），并保证顶层 type=object，
  这样 Kimi 永远不会收到带 $ref 的 schema。

接线方式：在 cc-switch 里把 Kimi For Coding 的 base_url 改成
  http://127.0.0.1:8787
（其余不动；cc-switch 设置的 Authorization 头会原样透传给 Kimi）。
启动本代理后再用 Codex。

测试用：环境变量 UPSTREAM 可覆盖真实上游（本地自测时指向 mock）。
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("PORT", "8787"))
UPSTREAM_BASE = os.environ.get("UPSTREAM", "https://api.kimi.com/coding/v1").rstrip("/")


def _resolve(root, ref_path):
    """按 JSON Pointer 在 root 文档里解析 '#/...' 引用；找不到返回 None。"""
    target = root
    for part in ref_path.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(target, dict):
            target = target.get(part)
        elif isinstance(target, list):
            try:
                target = target[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return target


def deref(node, root, seen=None):
    """深度复制 node，把所有指向本文档的 $ref 就地内联。

    - 有兄弟关键字的 $ref：把引用目标的内容合并进来，兄弟关键字优先
      （与 cc-switch PR #6627 语义一致）
    - 纯 $ref 节点：直接替换为引用目标的内容（对非 #/$defs/ 的引用同样适用，
      对应 PR #5125 的处理）
    - 循环引用：只取兄弟关键字部分，避免死循环
    """
    if seen is None:
        seen = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#"):
            key = ref[1:]
            target = _resolve(root, ref[1:]) if ref.startswith("#/") else None
            if isinstance(target, (dict, list)) and key not in seen:
                # 先把引用目标整体解引用（目标自身可能还带 $ref，如 __schema20 -> __schema7），
                # 再并入本节点 $ref 之外的兄弟关键字（兄弟优先）
                clone = dict(target) if isinstance(target, dict) else list(target)
                merged = deref(clone, root, seen | {key})
                for k, v in node.items():
                    if k != "$ref":
                        merged[k] = deref(v, root, seen | {key})
                return merged
            # 引用失效或成环：去掉 $ref，保留兄弟关键字
            return {k: deref(v, root, seen) for k, v in node.items() if k != "$ref"}
        return {k: deref(v, root, seen) for k, v in node.items()}
    if isinstance(node, list):
        return [deref(v, root, seen) for v in node]
    return node


def normalize_tools(body):
    """对请求体里的每个工具参数做规范化，原地修改 body。"""
    for tool in body.get("tools") or []:
        fn = tool.get("function")
        if not isinstance(fn, dict):
            continue
        params = fn.get("parameters")
        if not isinstance(params, dict):
            continue
        if "definitions" in params and "$defs" not in params:
            params["$defs"] = params.pop("definitions")
        fixed = deref(params, params)
        if not isinstance(fixed.get("type"), str):
            fixed["type"] = "object"
        fn["parameters"] = fixed
    return body


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self, raw_body):
        # cc-switch 会在 base_url 后拼接 /v1/chat/completions（或 /v1/responses），
        # 而 UPSTREAM_BASE 已含 /v1，去掉重复前缀，否则上游返回 404
        path = self.path
        if path.startswith("/v1/"):
            path = path[3:]
        target = UPSTREAM_BASE + path
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in ("host", "content-length", "transfer-encoding", "connection")
        }
        try:
            resp = requests.request(
                self.command,
                target,
                data=raw_body,
                headers=headers,
                stream=True,
                timeout=300,
            )
        except Exception as e:  # 上游不可达
            payload = json.dumps(
                {"error": {"message": "kimi-schema-fix middleware: %s" % e}}
            ).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                self.wfile.write(b"%x\r\n" % len(chunk))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            body = None
        if isinstance(body, dict):
            try:
                normalize_tools(body)
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            except Exception as e:
                print("[kimi-schema-fix] normalize failed, forwarding raw:", e)
        self._forward(raw)

    def do_GET(self):
        self._forward(None)

    def log_message(self, fmt, *args):
        path = self.path
        if path.startswith("/v1/"):
            path = path[3:]
        print("[kimi-schema-fix] %s %s -> %s" % (self.command, self.path, UPSTREAM_BASE + path))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        # 快速自检：构造 Codex 更新后的失败样例，验证解引用结果里没有 $ref
        sample = {
            "type": "object",
            "properties": {
                "bodyParts": {"$ref": "#/$defs/__schema20", "description": "部位列表"},
                "plain": {"type": "string"},
            },
            "$defs": {
                "__schema20": {
                    "$ref": "#/$defs/__schema7",
                    "type": "string",
                    "format": "uuid",
                    "minLength": 1,
                },
                "__schema7": {"type": "string", "format": "uuid"},
            },
        }
        fixed = deref(sample, sample)
        text = json.dumps(fixed)
        assert '"$ref"' not in text, "still has $ref after deref: %s" % text
        assert fixed["properties"]["bodyParts"]["type"] == "string"
        assert fixed["properties"]["bodyParts"]["description"] == "部位列表"
        assert fixed["$defs"]["__schema20"]["type"] == "string"
        assert fixed["$defs"]["__schema20"]["format"] == "uuid"
        assert fixed["$defs"]["__schema20"]["minLength"] == 1
        print("selfcheck OK: 样例中的 $ref 已全部内联，兄弟关键字保留")
        sys.exit(0)

    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print("[kimi-schema-fix] listening on http://%s:%d -> %s" % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE))
    print("[kimi-schema-fix] 在 cc-switch 中把 Kimi For Coding 的 base_url 改为 http://127.0.0.1:%d" % LISTEN_PORT)
    server.serve_forever()
