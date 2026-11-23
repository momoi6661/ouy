"""长期经验：Agent 做过的交易沉淀下来的教训，越用越准。

- 每笔仓平掉后自动复盘，写一条经验（当时怎么判断的、用了什么做法、结果怎样、为什么）。
- 新经验先是「观察」；后面的交易印证它 CONFIRMS_TO_RULE 次就升成「规矩」，推翻它 REFUTES_TO_DROP 次就删掉；
  STALE_DAYS 天没被用到：规矩降回观察，观察不再喂给 Worker（界面上还在，可以删）。
- 模拟盘和实盘的行情经验各存一套（模拟盘的行情和成交跟实盘没关系）；用户定的规矩两边共用。
"""
from __future__ import annotations

import difflib
from typing import Any

from .store import Store, now_ms

CONFIRMS_TO_RULE = 3
REFUTES_TO_DROP = 2
STALE_DAYS = 30
MAX_LESSONS = 80
MAX_TEXT = 300
DAY_MS = 86_400_000
GRADES = {"observe": "观察", "rule": "规矩"}
SHARED = "shared"


def _same(a: str, b: str) -> bool:
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.82


class Lessons:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _key(scope: str) -> str:
        return f"lessons:{scope}"

    def all(self, scope: str) -> list[dict[str, Any]]:
        """一个环境（demo / live）的行情经验，或者共用的规矩（shared）。顺手把太久没用到的降级。"""
        rows = self.store.get(self._key(scope), [])
        changed, now = False, now_ms()
        for row in rows:
            if scope == SHARED or now - row.get("used_ts", row["ts"]) < STALE_DAYS * DAY_MS:
                continue
            if row["grade"] == "rule":  # 规矩太久没用到：降回观察，重新攒印证
                row.update(grade="observe", confirms=min(row["confirms"], CONFIRMS_TO_RULE - 1), used_ts=now, note=f"{STALE_DAYS} 天没用到，从规矩降回观察")
                changed = True
            elif not row.get("stale"):
                row.update(stale=True, note=f"{STALE_DAYS} 天没用到，不再给 Worker 看")
                changed = True
        if changed:
            self.store.set(self._key(scope), rows)
        return rows

    def _save(self, scope: str, rows: list[dict[str, Any]]) -> None:
        if len(rows) > MAX_LESSONS:  # 放不下了：先扔过期的观察，再扔最久没用的观察；规矩留着
            rows.sort(key=lambda r: (r["grade"] == "rule", not r.get("stale"), r.get("used_ts", r["ts"])))
            rows = rows[len(rows) - MAX_LESSONS:]
            rows.sort(key=lambda r: r["id"])
        self.store.set(self._key(scope), rows)

    def get(self, scope: str, lesson_id: int) -> dict[str, Any] | None:
        return next((r for r in self.all(scope) if r["id"] == lesson_id), None)

    def add(self, scope: str, inst_id: str, text: str, *, source: str = "worker", grade: str = "observe", trade: dict[str, Any] | None = None) -> tuple[dict[str, Any], bool]:
        """记一条经验。和已有的某一条说的是同一件事，就算给那一条加一次印证，不重复记。返回 (这条经验, 是不是新的)。"""
        text = " ".join(text.split())[:MAX_TEXT]
        if len(text) < 6:
            raise ValueError("经验要写成一句完整的话：什么情况下、该怎么做 / 不该怎么做。")
        rows = self.all(scope)
        for row in rows:
            if row["inst_id"] == inst_id and _same(row["text"], text):
                return (self.judge(scope, row["id"], True, "又得出了同样的经验") or row), False
        seq = int(self.store.get("lessons_seq", 0)) + 1
        self.store.set("lessons_seq", seq)
        now = now_ms()
        row = {
            "id": seq, "inst_id": inst_id, "text": text, "grade": "rule" if scope == SHARED else grade, "confirms": 0, "refutes": 0,
            "ts": now, "used_ts": now, "source": source, "trade": trade, "stale": False, "enabled": True, "note": "",
        }
        self._save(scope, [*rows, row])
        return row, True

    def judge(self, scope: str, lesson_id: int, confirmed: bool, why: str = "") -> dict[str, Any] | None:
        """后面的交易印证了（confirmed=True）或推翻了这条经验。印证够次数升成规矩；推翻够次数删掉（返回 None 且不再存在）。"""
        rows = self.all(scope)
        row = next((r for r in rows if r["id"] == lesson_id), None)
        if row is None:
            return None
        row["used_ts"], row["stale"] = now_ms(), False
        if confirmed:
            row["confirms"] += 1
            row["note"] = f"被印证：{why}"[:200] if why else row.get("note", "")
            if row["grade"] == "observe" and row["confirms"] >= CONFIRMS_TO_RULE:
                row.update(grade="rule", note=f"被印证 {row['confirms']} 次，升成规矩")
        else:
            row["refutes"] += 1
            row["note"] = f"被推翻：{why}"[:200] if why else row.get("note", "")
            if scope != SHARED and row["refutes"] >= REFUTES_TO_DROP:
                self._save(scope, [r for r in rows if r["id"] != lesson_id])
                return {**row, "dropped": True}
            if row["grade"] == "rule" and scope != SHARED:  # 规矩被推翻一次：先降回观察
                row.update(grade="observe", confirms=min(row["confirms"], CONFIRMS_TO_RULE - 1), note=f"被推翻：{why}，从规矩降回观察"[:200])
        self._save(scope, rows)
        return row

    def edit(
        self, scope: str, lesson_id: int, *, text: str | None = None, grade: str | None = None, inst_id: str | None = None, enabled: bool | None = None,
    ) -> dict[str, Any] | None:
        """用户在界面上改一条经验：改写、手动升成规矩 / 降回观察、或者暂停 / 恢复启用（不删记忆）。"""
        rows = self.all(scope)
        row = next((r for r in rows if r["id"] == lesson_id), None)
        if row is None:
            return None
        if text is not None and " ".join(text.split()):
            row["text"] = " ".join(text.split())[:MAX_TEXT]
        if grade in GRADES and scope != SHARED:
            row["grade"] = grade
        if inst_id is not None:
            row["inst_id"] = inst_id
        if enabled is not None:
            row["enabled"] = bool(enabled)
            row["note"] = "已恢复启用" if enabled else "已手动暂停，暂不喂给 Worker"
        else:
            row.update(used_ts=now_ms(), stale=False, note="你手动改过")
        self._save(scope, rows)
        return row

    def delete(self, scope: str, lesson_id: int) -> bool:
        rows = self.all(scope)
        left = [r for r in rows if r["id"] != lesson_id]
        if len(left) == len(rows):
            return False
        self._save(scope, left)
        return True

    def brief(self, env: str, inst_id: str, rules: int = 8, observes: int = 6) -> str:
        """给 Worker 看的经验：只取启用中（enabled=True）且未过期的条目；未获印证的零散观察最多带 4 条，防止刷屏淹没核心规矩。"""
        mine = [r for r in self.all(env) if r["inst_id"] in ("", inst_id) and not r.get("stale") and r.get("enabled", True)]
        by_recent = lambda rows: sorted(rows, key=lambda r: r.get("used_ts", r["ts"]), reverse=True)  # noqa: E731
        shared_on = [r for r in self.all(SHARED) if r.get("enabled", True)]
        out = [f"- [你定的规矩] {r['text']}" for r in shared_on[-rules:]]
        out += [f"- #{r['id']} [规矩，印证 {r['confirms']} 次] {r['text']}" for r in by_recent([r for r in mine if r["grade"] == "rule"])[:rules]]
        obs_all = sorted(
            [r for r in mine if r["grade"] == "observe"],
            key=lambda r: (r.get("confirms", 0) > 0, r.get("used_ts", r["ts"])),
            reverse=True,
        )
        picked_obs: list[dict[str, Any]] = []
        zero_cnt = 0
        for r in obs_all:
            if len(picked_obs) >= observes:
                break
            if r.get("confirms", 0) == 0:
                if zero_cnt >= 4:
                    continue
                zero_cnt += 1
            picked_obs.append(r)
        out += [
            f"- #{r['id']} [观察，印证 {r['confirms']} / 推翻 {r['refutes']}] {r['text']}"
            for r in picked_obs
        ]
        return "\n".join(out)
