"""按打法记战绩：程序每次进场记下「这一单用的哪条打法」，平仓后把已实现盈亏记到那条打法头上。
模拟盘和实盘分开记。目的只有一个：攒够笔数以后，能看出哪条打法在赚、哪条在亏，而不是凭感觉。"""
from __future__ import annotations

from typing import Any

from .store import Store, now_ms

UNTAGGED = "未标记（手动或 Worker 直接下单）"
UNKNOWN_ORIGIN = "没记来源（手动、Worker 直接下单或旧记录）"
MIN_SAMPLE = 20  # 不到这么多笔不下结论
TAG_KEEP_MS = 14 * 86_400_000


class Scoreboard:
    def __init__(self, store: Store):
        self.store = store

    def _since(self, env: str) -> int:
        since = self.store.get(f"score_since:{env}")
        if not since:  # 从开始记的那一刻算起，更早的历史成交不知道用的什么打法，不往里算
            since = now_ms()
            self.store.set(f"score_since:{env}", since)
        return since

    def tag(self, env: str, inst_id: str, side: str, strategy: str, via: str, *, ttl_minutes: float = 3, opened_ts: int | None = None, origin: str = "") -> None:
        """记一笔进场。ttl_minutes：这张单最晚多久之内成交（限价单按它的有效期）；opened_ts：接手一笔已经开着的仓时填它的开仓时间；
        origin：这笔单是谁的主意（快判 / 照算法候选 / 改过算法候选 / Worker 自拟），平仓后另记一份按来源的战绩。"""
        self._since(env)
        tags = [t for t in self.store.get(f"score_tags:{env}", []) if now_ms() - t["ts"] < TAG_KEEP_MS]
        tags.append({"ts": now_ms(), "inst_id": inst_id, "side": side, "strategy": (strategy or "").strip() or "没写打法", "via": via,
                     "ttl": int(ttl_minutes * 60_000), "opened_ts": opened_ts, "origin": origin})
        self.store.set(f"score_tags:{env}", tags[-300:])

    @staticmethod
    def _match(tags: list[dict[str, Any]], inst_id: str, trade: Any) -> dict[str, Any] | None:
        best = None
        for tag in tags:
            if tag["inst_id"] != inst_id or tag["side"] != trade.side:
                continue
            if tag.get("opened_ts"):
                ok = abs(tag["opened_ts"] - trade.opened_ts) <= 120_000
            else:
                ok = -10_000 <= trade.opened_ts - tag["ts"] <= tag["ttl"]
            if ok and (best is None or tag["ts"] > best["ts"]):
                best = tag
        return best

    async def settle(self, env: str, trader: Any, instruments: list[str]) -> list[dict[str, Any]]:
        """查新平掉的仓，记进各自打法的战绩。返回这次新记上的几笔。"""
        since, tags = self._since(env), self.store.get(f"score_tags:{env}", [])
        seen: list[str] = self.store.get(f"score_seen:{env}", [])
        stats: dict[str, dict[str, Any]] = self.store.get(f"score_stats:{env}", {})
        origins: dict[str, dict[str, Any]] = self.store.get(f"score_origins:{env}", {})
        done: list[dict[str, Any]] = []
        for inst_id in sorted({t["inst_id"] for t in tags} | set(instruments)):
            try:
                trades = await trader.history(inst_id, 30, env)
            except Exception:  # noqa: BLE001 - 这个币的历史查不到就下次再记
                continue
            for trade in sorted(trades, key=lambda t: t.closed_ts):
                key = f"{inst_id}|{trade.side}|{trade.opened_ts}|{trade.closed_ts}"
                if key in seen or trade.closed_ts < since:
                    continue
                tag = self._match(tags, inst_id, trade)
                name = tag["strategy"] if tag else UNTAGGED
                origin = (tag.get("origin") if tag else "") or UNKNOWN_ORIGIN
                for book, key_name in ((stats, name), (origins, origin)):
                    s = book.setdefault(key_name, {"n": 0, "wins": 0, "pnl": 0.0, "best": 0.0, "worst": 0.0, "last_ts": 0})
                    s.update(n=s["n"] + 1, wins=s["wins"] + (1 if trade.pnl > 0 else 0), pnl=round(s["pnl"] + trade.pnl, 4),
                             best=round(max(s["best"], trade.pnl), 4), worst=round(min(s["worst"], trade.pnl), 4), last_ts=trade.closed_ts)
                seen.append(key)
                done.append({"inst_id": inst_id, "strategy": name, "via": tag["via"] if tag else "", "origin": origin, "pnl": round(trade.pnl, 4), "side": trade.side})
        if done:
            self.store.set(f"score_seen:{env}", seen[-800:])
            self.store.set(f"score_stats:{env}", stats)
            self.store.set(f"score_origins:{env}", origins)
        return done

    def stats(self, env: str) -> dict[str, dict[str, Any]]:
        return self.store.get(f"score_stats:{env}", {})

    def origins(self, env: str) -> dict[str, dict[str, Any]]:
        """按「谁的主意」分的战绩：Worker 的取舍（改过 / 自拟）比照算法候选做强还是弱，看这张表。"""
        return self.store.get(f"score_origins:{env}", {})

    @staticmethod
    def line(s: dict[str, Any]) -> str:
        text = f"{s['n']} 笔，赢 {s['wins']} 笔（{s['wins'] / s['n'] * 100:.0f}%），累计 {s['pnl']:+.2f} USDT"
        if s["n"] < MIN_SAMPLE:
            return text + f"；样本不到 {MIN_SAMPLE} 笔，只作参考"
        return text + ("；样本够了而且在亏：先找出亏在哪再用" if s["pnl"] < 0 else "；样本够了而且在赚")

    def of(self, env: str, name: str) -> str:
        """某条打法的战绩；名字按开头匹配（Worker 写的是「前高前低法」，文档标题是「前高前低法（上一段的最高点和最低点）」）。"""
        short = name.split("（")[0].strip()
        for key, s in self.stats(env).items():
            if short and (key.startswith(short) or short.startswith(key.split("（")[0])):
                return self.line(s)
        return ""

    def brief(self, env: str) -> str:
        rows = sorted(self.stats(env).items(), key=lambda kv: -kv[1]["n"])
        return "\n".join(f"- {name}：{self.line(s)}" for name, s in rows)
