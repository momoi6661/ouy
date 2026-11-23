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


def _patch_amend_algos(source: str) -> str:
    begin = source.find('name: "swap_amend_algo_order",')
    end = source.find("return normalizeResponse(response);", begin)
    if begin == -1 or end == -1:
        return source
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
            return source
        patched = patched.replace(schema, schema + f'\n          {field}: {{ type: "string", enum: ["last", "mark", "index"] }},', 1)
        patched = patched.replace(handler, handler + f'\n            {field}: readString(args, "{field}"),', 1)
    return source if patched == tool else source[:begin] + patched + source[end:]


def _patch_oauth_singleflight(source: str) -> str:
    """修复官方 MCP 子进程两个掉登录缺陷：
    1. resolveAccessToken 在限流器排队后并发唤醒 okx-auth，导致一次性 refresh_token 并发重放作废；改为进程内单飞复用同一个 Promise。
    2. 收到 HTTP 401 时立即作废进程内的 60 秒旧令牌缓存，避免继续拿已失效令牌请求整整一分钟。"""
    old_resolve = (
        "  async resolveAccessToken() {\n"
        "    if (this.cachedAccessToken && Date.now() - this.cachedAccessTokenAt < TOKEN_CACHE_TTL_MS) {\n"
        "      return this.cachedAccessToken;\n"
        "    }\n"
        "    try {\n"
        "      const token = await execAuthToken();\n"
        "      this.cachedAccessToken = token;\n"
        "      this.cachedAccessTokenAt = Date.now();\n"
        "      return token;\n"
        "    } catch (e) {\n"
        "      if (e instanceof NotLoggedInError) {\n"
        "        return null;\n"
        "      }\n"
        "      throw e;\n"
        "    }\n"
        "  }"
    )
    new_resolve = (
        "  async resolveAccessToken() {\n"
        "    if (this.cachedAccessToken && Date.now() - this.cachedAccessTokenAt < TOKEN_CACHE_TTL_MS) {\n"
        "      return this.cachedAccessToken;\n"
        "    }\n"
        "    if (this._tokenInflight) {\n"
        "      return this._tokenInflight;\n"
        "    }\n"
        "    this._tokenInflight = (async () => {\n"
        "      try {\n"
        "        const token = await execAuthToken();\n"
        "        this.cachedAccessToken = token;\n"
        "        this.cachedAccessTokenAt = Date.now();\n"
        "        return token;\n"
        "      } catch (e) {\n"
        "        if (e instanceof NotLoggedInError) {\n"
        "          return null;\n"
        "        }\n"
        "        throw e;\n"
        "      } finally {\n"
        "        this._tokenInflight = null;\n"
        "      }\n"
        "    })();\n"
        "    return this._tokenInflight;\n"
        "  }"
    )
    if "_tokenInflight" not in source and old_resolve in source:
        source = source.replace(old_resolve, new_resolve, 1)
    old_401 = (
        "    if (!response.ok) {\n"
        '      this.logResponse(response.status, rawText.length, elapsed, traceId, parsed.code ?? "-", parsed.msg);'
    )
    new_401 = (
        "    if (!response.ok) {\n"
        "      if (response.status === 401) {\n"
        "        this.cachedAccessToken = void 0;\n"
        "        this.cachedAccessTokenAt = 0;\n"
        "      }\n"
        '      this.logResponse(response.status, rawText.length, elapsed, traceId, parsed.code ?? "-", parsed.msg);'
    )
    if "this.cachedAccessToken = void 0;" not in source and old_401 in source:
        source = source.replace(old_401, new_401, 1)
    return source


def mcp_command() -> tuple[str, list[str]]:
    """Restore the two amend-algos fields and patch OAuth token single-flight in the pinned Trade Kit."""
    command = Path(find_bin("okx-trade-mcp"))
    module = Path("@okx_ai/okx-trade-mcp/dist/index.js")
    candidates = (ROOT / "node_modules" / module, command.parent / "node_modules" / module, command.resolve())
    entry = next((path for path in candidates if path.name == "index.js" and path.is_file()), None)
    node = shutil.which("node")
    if entry is None or node is None:
        return str(command), []

    source = entry.read_text(encoding="utf-8")
    content = _patch_oauth_singleflight(_patch_amend_algos(source))
    if content == source:
        return str(command), []

    target = entry.with_name("index.ouy-amend.mjs")
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
