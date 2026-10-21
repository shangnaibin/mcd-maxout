"""MCP 客户端：用标准库直连麦当劳 MCP Server（streamable HTTP + JSON-RPC）。

不依赖任何第三方库，token 只从环境变量读取，绝不写进代码或配置文件。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"


class MCPError(RuntimeError):
    """MCP 调用失败。"""


def _parse_sse(raw: str) -> str:
    """从 SSE 流里取出最后一条 data 负载。"""
    payload = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
    if payload is None:
        raise MCPError("SSE 响应里没有 data 负载")
    return payload


def _extract_json(text: str) -> dict:
    """从工具返回的 Markdown 里刨出真 JSON。"""
    marker = "## Original Response"
    if marker in text:
        text = text.split(marker, 1)[1]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise MCPError(f"返回内容里找不到 JSON：{text[:120]}")
    return json.loads(text[start : end + 1])


class MCDClient:
    def __init__(self, token: str | None = None, endpoint: str = DEFAULT_ENDPOINT, timeout: int = 60):
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "")
        if not self.token:
            raise MCPError(
                "缺少 MCD_MCP_TOKEN 环境变量。\n"
                "去 https://open.mcd.cn 用手机号登录，控制台里激活并复制 MCP Token，然后：\n"
                "  export MCD_MCP_TOKEN=你的token"
            )
        self.endpoint = endpoint
        self.timeout = timeout
        self._id = 0
        self._session: str | None = None
        self._initialized = False
        self.calls: list[str] = []

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.endpoint, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        if self._session:
            req.add_header("Mcp-Session-Id", self._session)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session = sid
                raw = resp.read().decode("utf-8", "replace")
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            raise MCPError(f"HTTP {exc.code}：{exc.read().decode('utf-8', 'replace')[:200]}") from exc
        except urllib.error.URLError as exc:
            raise MCPError(f"网络不通：{exc.reason}") from exc
        if "text/event-stream" in ctype:
            raw = _parse_sse(raw)
        return json.loads(raw)

    def _ensure_init(self) -> None:
        if self._initialized:
            return
        self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "mcd-maxout", "version": "0.1.0"},
                },
            }
        )
        self._initialized = True

    def call(self, tool: str, **arguments):
        self._ensure_init()
        self._id += 1
        self.calls.append(tool)
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        if "error" in resp:
            raise MCPError(f"{tool} 调用出错：{resp['error']}")
        result = resp.get("result", {})
        payload = result.get("structuredContent")
        if payload is None:
            text = "".join(part.get("text", "") for part in result.get("content", []))
            payload = _extract_json(text)
        if not isinstance(payload, dict) or not payload.get("success", True):
            message = payload.get("message") if isinstance(payload, dict) else "未知错误"
            raise MCPError(f"{tool} 返回失败：{message}")
        return payload.get("data")
