"""欧易官方 Agent 授权：封装欧易官方 CLI 的 OAuth 设备授权流程。

后端只拿到「授权页面链接 + 验证码」，令牌由官方组件保存在本机，后端和前端都接触不到。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from .runtime import child_env, find_bin


log = logging.getLogger("ouy.auth")

# 欧易在用户授权前要求两次查询至少间隔 5 秒；查得太勤或同时查会让授权失败
POLL_INTERVAL = 5.0


class OkxAuthError(RuntimeError):
    pass


def _redact(text: str) -> str:
    """去掉可能是令牌 / 授权码的长串，只留下能看懂的报错。"""
    return re.sub(r"[A-Za-z0-9_\-\.]{24,}", "<已隐藏>", text).strip()


def _auth_binary() -> str | None:
    """官方 CLI 下载到本机的授权程序。直接调用它才能拿到失败原因（CLI 包装层会把报错吞掉）。"""
    path = Path.home() / ".okx" / "bin" / ("okx-auth.exe" if sys.platform == "win32" else "okx-auth")
    return str(path) if path.is_file() else None


def _last_json(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class OkxAuth:
    def __init__(self, settings: Settings, *, auth_lock: asyncio.Lock | None = None):
        self.settings = settings
        self._lock = asyncio.Lock()
        self._status_lock = asyncio.Lock()
        self._auth_lock = auth_lock or asyncio.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self.last_error = ""

    async def _run(self, *args: str, timeout: float = 60, binary: str | None = None) -> tuple[int, str, str]:
        async with self._auth_lock:
            proc = await asyncio.create_subprocess_exec(
                binary or find_bin("okx"),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env(self.settings),
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise OkxAuthError("欧易授权组件响应超时，请检查网络或代理设置。") from exc
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def status(self) -> dict[str, Any]:
        """当前授权状态。等待授权期间每次查询都会向欧易确认一次，所以这里排队并限速。"""
        async with self._status_lock:
            before = self._cached
            ttl = 60 if before and before["status"] == "logged_in" else POLL_INTERVAL
            if before and time.monotonic() - self._cached_at < ttl:
                return before
            binary = _auth_binary()
            if binary:
                _, out, err = await self._run("status", "--json", timeout=30, binary=binary)
            else:
                _, out, err = await self._run("auth", "status", "--json", timeout=30)
            data = _last_json(out)
            if data is None:
                raise OkxAuthError(_redact(err or out)[-300:] or "无法读取欧易授权状态。")
            status = data.get("status", "not_logged_in")
            if status == "logged_in":
                self.last_error = ""
            elif before and before["status"] == "pending" and status != "pending" and (before.get("ttl") or 0) > POLL_INTERVAL * 2:
                # 授权码还没过期，等待状态却没了：欧易拒绝了这次授权，或者令牌没能保存下来
                self.last_error = _redact(err)[-400:] or "欧易没有返回具体原因"
                log.warning("欧易授权没有完成：%s", self.last_error)
            self._cached = {
                "status": status,
                "site": data.get("site"),
                "scopes": data.get("scopes"),
                "verificationUri": data.get("verificationUri"),
                "userCode": data.get("userCode"),
                "ttl": data.get("ttl"),
                "apiKey": bool(data.get("apiKey")),
                "error": self.last_error or None,
            }
            self._cached_at = time.monotonic()
            return self._cached

    async def login(self) -> dict[str, Any]:
        """发起授权，返回 verificationUri / userCode / expiresIn。"""
        async with self._lock:
            self.last_error = ""
            current = await self.status()
            if current["status"] == "logged_in":
                return current
            if current["status"] == "pending" and current.get("userCode") and (current.get("ttl") or 0) > 30:
                return {**current, "expiresIn": current["ttl"]}
            _, out, err = await self._run(
                "auth", "login", "--manual", "--site", self.settings.okx_site, timeout=90
            )
            data = _last_json(out)
            if data and data.get("status") == "skipped":
                raise OkxAuthError(
                    "本机 ~/.okx/config.toml 里配置了 API Key，官方组件会优先使用它而跳过授权。"
                    "请先移除该配置再授权。"
                )
            if not data or not data.get("userCode"):
                raise OkxAuthError(_redact(err or out)[-300:] or "向欧易申请授权失败。")
            async with self._status_lock:
                self._cached = None
            return {
                "status": "pending",
                "site": self.settings.okx_site,
                "verificationUri": data["verificationUri"],
                "userCode": data["userCode"],
                "expiresIn": data.get("expiresIn", 600),
            }

    async def logout(self) -> None:
        async with self._lock:
            code, out, err = await self._run("auth", "logout", timeout=30)
            async with self._status_lock:
                self._cached = None
            self.last_error = ""
            if code != 0:
                raise OkxAuthError((err or out).strip()[-300:] or "退出授权失败。")
