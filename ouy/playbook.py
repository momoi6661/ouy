"""策略库：Agent（和用户）自己写的、可以反复用的策略。

执行器（strategy.py）只认 6 种写死的执行方式（突破、回踩、区间、管理仓位、翻倍补仓、网格）。策略库在它上面一层：
一条策略 = 起个名字 + 什么行情用 + 触发价怎么定 + 止损止盈怎么定 + 一组默认参数，底下用哪种执行方式去跑。
Worker 把反复有效的做法写成一条存进来，之后布置时点名用它；每条策略在模拟盘和实盘各记一份战绩，表现差的自动停用。
"""
from __future__ import annotations

import re
from typing import Any

from .store import Store, now_ms
from .strategy import KINDS

MAX_PLAYBOOKS = 30
#: 可以存成默认值的参数：都是「怎么做」的规则，不含具体价位（价位每次按当时的行情定）
DEFAULT_KEYS = (
    "leverage", "risk_pct", "margin_usdt", "expires_minutes", "breakeven_r", "partial_r", "partial_fraction", "trail_pct", "max_hold_minutes",
    "max_rounds", "base_margin", "step_pct", "multiplier", "max_layers", "take_profit_pct", "grids", "margin_per_grid",
)
#: 做够这么多笔、胜率还这么低而且总体亏钱，就自动停用
AUTO_OFF_MIN_TRADES, AUTO_OFF_WIN_RATE = 5, 0.3


class PlaybookError(ValueError):
    pass


class Playbooks:
    KEY = "playbooks"

    def __init__(self, store: Store):
        self.store = store

    def all(self) -> list[dict[str, Any]]:
        return list(self.store.get(self.KEY, []))

    def get(self, name: str) -> dict[str, Any] | None:
        key = name.strip().lower()
        return next((p for p in self.all() if p["name"].lower() == key), None)

    def write(self, name: str, base: str, when: str, entry: str, exit: str, defaults: dict[str, Any] | None = None, author: str = "用户") -> tuple[dict[str, Any], bool]:
        """新写一条，或者改写同名的一条（版本号加一，战绩保留）。返回 (这条策略, 是不是新建的)。"""
        name = re.sub(r"\s+", " ", name).strip()[:24]
        if not name:
            raise PlaybookError("策略要有名字。")
        if base not in KINDS:
            kinds = "、".join(f"{k}（{v['label']}）" for k, v in KINDS.items())
            raise PlaybookError(f"base 只能是这几种执行方式之一：{kinds}。")
        if not when.strip() or not (entry.strip() or base == "manage") or not exit.strip():
            raise PlaybookError("要写清楚三件事：什么行情用（when）、触发价怎么定（entry）、止损止盈怎么定（exit）。")
        clean: dict[str, Any] = {}
        for key, value in (defaults or {}).items():
            if value is None:
                continue
            if key not in DEFAULT_KEYS:
                raise PlaybookError(f"默认参数里不能有 {key}：具体价位每次按当时的行情定，这里只存规则类的参数（{'、'.join(DEFAULT_KEYS)}）。")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise PlaybookError(f"默认参数 {key} 要是大于 0 的数。")
            clean[key] = value
        rows = self.all()
        old = next((p for p in rows if p["name"].lower() == name.lower()), None)
        if old is None and len(rows) >= MAX_PLAYBOOKS:
            raise PlaybookError(f"策略库最多 {MAX_PLAYBOOKS} 条，先删掉不用的。")
        row = {
            "name": name, "base": base, "when": when.strip()[:300], "entry": entry.strip()[:400], "exit": exit.strip()[:400], "defaults": clean,
            "author": old["author"] if old else author, "created_ts": old["created_ts"] if old else now_ms(), "updated_ts": now_ms(),
            "updated_by": author, "version": (old["version"] + 1) if old else 1, "enabled": True, "off_reason": "",
            "stats": old["stats"] if old else {},
        }
        self.store.set(self.KEY, [row if p is old else p for p in rows] if old else [*rows, row])
        return row, old is None

    def delete(self, name: str) -> bool:
        row = self.get(name)
        if row is None:
            return False
        self.store.set(self.KEY, [p for p in self.all() if p["name"] != row["name"]])
        return True

    def set_enabled(self, name: str, on: bool, reason: str = "") -> dict[str, Any] | None:
        row = self.get(name)
        if row is None:
            return None
        row.update(enabled=on, off_reason="" if on else reason[:200])
        self.store.set(self.KEY, [row if p["name"] == row["name"] else p for p in self.all()])
        return row

    def record(self, name: str, env: str, pnl: float) -> dict[str, Any] | None:
        """用这条策略做完了一笔：记进它在这个环境里的战绩。模拟盘和实盘分开记，互不影响。做得太差自动停用。"""
        row = self.get(name)
        if row is None:
            return None
        s = row["stats"].setdefault(env, {"n": 0, "wins": 0, "pnl": 0.0, "last_ts": 0})
        s.update(n=s["n"] + 1, wins=s["wins"] + (1 if pnl > 0 else 0), pnl=round(s["pnl"] + pnl, 4), last_ts=now_ms())
        if row["enabled"] and s["n"] >= AUTO_OFF_MIN_TRADES and s["wins"] / s["n"] < AUTO_OFF_WIN_RATE and s["pnl"] < 0:
            row.update(enabled=False, off_reason=f"在{'模拟盘' if env == 'demo' else '实盘'}做了 {s['n']} 笔只赢 {s['wins']} 笔、累计 {s['pnl']:+.2f} USDT，自动停用。改写之后会重新启用。")
        self.store.set(self.KEY, [row if p["name"] == row["name"] else p for p in self.all()])
        return row

    @staticmethod
    def KINDS_LABEL(base: str) -> str:  # noqa: N802 - 给工具拼提示用
        return KINDS[base]["label"]

    @staticmethod
    def score(row: dict[str, Any], env: str) -> str:
        s = (row.get("stats") or {}).get(env)
        if not s or not s["n"]:
            return "还没用过"
        return f"{s['n']} 笔，赢 {s['wins']} 笔（{s['wins'] / s['n'] * 100:.0f}%），累计 {s['pnl']:+.2f} USDT"

    def brief(self, env: str) -> str:
        """给 Worker 看的策略库：每条怎么用、在当前环境里的战绩。"""
        rows = self.all()
        if not rows:
            return "（空的。反复有效的做法可以用 write_strategy 写成一条存进来。）"
        where = "模拟盘" if env == "demo" else "实盘"
        lines = []
        for p in rows:
            defaults = "，".join(f"{k}={v:g}" for k, v in p["defaults"].items())
            state = "" if p["enabled"] else f"【已停用：{p['off_reason'] or '手动停用'}】"
            lines.append(
                f"- 「{p['name']}」{state}（{KINDS[p['base']]['label']}，第 {p['version']} 版，{p['author']} 写的）{where}战绩：{self.score(p, env)}\n"
                f"  什么行情用：{p['when']}\n  触发价怎么定：{p['entry'] or '—'}\n  止损止盈：{p['exit']}" + (f"\n  默认参数：{defaults}" if defaults else "")
            )
        return "\n".join(lines)
