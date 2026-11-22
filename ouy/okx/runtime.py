"""定位欧易官方 Agent Trade Kit 可执行文件，并准备子进程环境。"""
from __future__ import annotations

import os
import shutil
import sys

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


def child_env(settings: Settings) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _BLOCKED_ENV}
    env["OKX_SITE"] = settings.okx_site
    env.setdefault("OKX_LOG_RETENTION_DAYS", "3")  # 官方组件自己的调用日志（~/.okx/logs）只留 3 天
    if settings.proxy_url:
        env["HTTPS_PROXY"] = settings.proxy_url
        env["HTTP_PROXY"] = settings.proxy_url
    return env
