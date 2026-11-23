"""定位欧易官方 Agent Trade Kit 可执行文件，并准备子进程环境。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from ..config import ROOT, Settings

# 这些变量一旦存在，官方组件会改用 API Key 而跳过 OAuth 授权，所以一律不向子进程传递。
_BLOCKED_ENV = ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")


class OkxRuntimeMissing(RuntimeError):
    pass


def find_bin(name: str) -> str:
    suffixes = (".cmd", ".exe", "") if sys.platform == "win32" else ("",)
    for suffix in suffixes:
        local = ROOT / "node_modules" / ".bin" / f"{name}{suffix}"
        if local.is_file():
            return str(local)
    found = shutil.which(name)
    if found:
        return found
    raise OkxRuntimeMissing(
        f"找不到 {name}。请在项目根目录执行 npm install 安装欧易官方组件（需要 Node.js 18+）。"
    )


def mcp_command() -> tuple[str, list[str]]:
    """Restore the two amend-algos fields missing from the pinned Trade Kit."""
    command = Path(find_bin("okx-trade-mcp"))
    module = Path("@okx_ai/okx-trade-mcp/dist/index.js")
    candidates = (ROOT / "node_modules" / module, command.parent / "node_modules" / module, command.resolve())
    entry = next((path for path in candidates if path.name == "index.js" and path.is_file()), None)
    node = shutil.which("node")
    if entry is None or node is None:
        return str(command), []

    source = entry.read_text(encoding="utf-8")
    begin = source.find('name: "swap_amend_algo_order",')
    end = source.find("return normalizeResponse(response);", begin)
    if begin == -1 or end == -1:
        return str(command), []
    tool = source[begin:end]
    patched = tool
    for leg in ("Tp", "Sl"):
        field = f"new{leg}TriggerPxType"
        if field in patched:
            continue
        price = f"new{leg}TriggerPx"
        schema = f'{price}: {{ type: "string", description: "New {leg.upper()} trigger price" }},'
        handler = f'{price}: readString(args, "{price}"),'
        if schema not in patched or handler not in patched:
            return str(command), []
        patched = patched.replace(schema, schema + f'\n          {field}: {{ type: "string", enum: ["last", "mark", "index"] }},', 1)
        patched = patched.replace(handler, handler + f'\n            {field}: readString(args, "{field}"),', 1)
    if patched == tool:
        return str(command), []

    target = entry.with_name("index.ouy-amend.mjs")
    content = source[:begin] + patched + source[end:]
    if not target.is_file() or target.read_text(encoding="utf-8") != content:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=entry.parent, delete=False) as handle:
            handle.write(content)
        temp = Path(handle.name)
        try:
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
    return node, [str(target)]


def child_env(settings: Settings) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _BLOCKED_ENV}
    env["OKX_SITE"] = settings.okx_site
    env.setdefault("OKX_LOG_RETENTION_DAYS", "3")  # 官方组件自己的调用日志（~/.okx/logs）只留 3 天
    if settings.proxy_url:
        env["HTTPS_PROXY"] = settings.proxy_url
        env["HTTP_PROXY"] = settings.proxy_url
    return env
