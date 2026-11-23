"""SQLite 持久化：配置、Agent 日志、待确认指令、权益曲线。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, env TEXT NOT NULL,
  inst_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS journal_ts ON journal (ts DESC);
CREATE TABLE IF NOT EXISTS pending (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, env TEXT NOT NULL,
  action TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', result TEXT NOT NULL DEFAULT '{}', resolved_ts INTEGER
);
CREATE TABLE IF NOT EXISTS lots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, env TEXT NOT NULL, inst_id TEXT NOT NULL, side TEXT NOT NULL,
  style TEXT NOT NULL, contracts REAL NOT NULL, entry_px REAL NOT NULL, stop_loss REAL, take_profit REAL,
  algo_id TEXT NOT NULL DEFAULT '', ord_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open', closed_ts INTEGER, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS work_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL, env TEXT NOT NULL,
  inst_id TEXT NOT NULL DEFAULT '', source TEXT NOT NULL, goal TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
  model TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS work_items_ts ON work_items (env, ts DESC);
CREATE TABLE IF NOT EXISTS strategies (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL, env TEXT NOT NULL, inst_id TEXT NOT NULL,
  kind TEXT NOT NULL, side TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'armed', params TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS strategies_live ON strategies (env, status);
CREATE TABLE IF NOT EXISTS activity (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, env TEXT NOT NULL, inst_id TEXT NOT NULL DEFAULT '',
  agent TEXT NOT NULL, work_id INTEGER, kind TEXT NOT NULL, text TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS activity_ts ON activity (env, id DESC);
CREATE TABLE IF NOT EXISTS equity (ts INTEGER NOT NULL, env TEXT NOT NULL, equity REAL NOT NULL);
CREATE INDEX IF NOT EXISTS equity_ts ON equity (env, ts);
CREATE TABLE IF NOT EXISTS algo_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, env TEXT NOT NULL, inst TEXT NOT NULL, algo TEXT NOT NULL,
  side TEXT NOT NULL DEFAULT '', px REAL NOT NULL DEFAULT 0, atr REAL NOT NULL DEFAULT 0, text TEXT NOT NULL, effect TEXT NOT NULL DEFAULT '',
  checks TEXT NOT NULL DEFAULT '{}', done INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS algo_events_ts ON algo_events (env, inst, id DESC);
CREATE TABLE IF NOT EXISTS opening_tape (
  env TEXT NOT NULL, inst TEXT NOT NULL, minute INTEGER NOT NULL, ts INTEGER NOT NULL,
  first REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, last REAL NOT NULL, samples INTEGER NOT NULL,
  context TEXT NOT NULL, PRIMARY KEY(env, inst, minute)
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(pending)")}
            if "preview" not in columns:  # 旧版本建的库没有这一列
                self._db.execute("ALTER TABLE pending ADD COLUMN preview TEXT NOT NULL DEFAULT '{}'")
            if "data" not in {row["name"] for row in self._db.execute("PRAGMA table_info(algo_events)")}:  # 旧库：算法事件还没有「依据」这一列
                self._db.execute("ALTER TABLE algo_events ADD COLUMN data TEXT NOT NULL DEFAULT '{}'")
            self._db.commit()

    def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur

    def _rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # ----- 算法事件：哪个算法在什么价位算出了什么、触发了什么，之后价格有没有照它说的走 -----
    def add_algo_event(self, env: str, inst: str, algo: str, side: str, px: float, atr: float, text: str, effect: str,
                       data: dict[str, Any] | None = None, score: bool = True) -> int:
        """data = 算出这个结论用到的数（事后能查）；score=False = 这条不是方向判断，之后不核对对错。"""
        cur = self._exec("INSERT INTO algo_events (ts, env, inst, algo, side, px, atr, text, effect, done, data) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (now_ms(), env, inst, algo, side, px, atr, text[:400], effect[:300], 0 if side and px and score else 1,
                          json.dumps(data or {}, ensure_ascii=False, default=str)))
        return int(cur.lastrowid)

    def algo_events(self, env: str, inst: str | None = None, limit: int = 80, pending: bool = False) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM algo_events WHERE env=?", [env]
        if inst:
            sql += " AND inst=?"
            params.append(inst)
        if pending:
            sql += " AND done=0"
        return [{**dict(r), "checks": json.loads(r["checks"] or "{}"), "data": json.loads(r["data"] or "{}")}
                for r in self._rows(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))]

    def check_algo_event(self, event_id: int, checks: dict[str, Any], done: bool) -> None:
        self._exec("UPDATE algo_events SET checks=?, done=? WHERE id=?", (json.dumps(checks, ensure_ascii=False), 1 if done else 0, event_id))

    # ----- kv -----
    def record_quote(self, env: str, inst: str, ts: int, px: float, context: dict) -> None:
        self._exec("""INSERT INTO opening_tape VALUES (?,?,?,?,?,?,?,?,1,?)
          ON CONFLICT(env,inst,minute) DO UPDATE SET ts=excluded.ts, high=max(high,excluded.high),
          low=min(low,excluded.low), last=excluded.last, samples=samples+1, context=excluded.context
          WHERE excluded.ts>opening_tape.ts""", (env, inst, ts // 60_000 * 60_000, ts, px, px, px, px, json.dumps(context)))

    def opening_minutes(self, env: str, inst: str, limit: int = 6) -> list[dict[str, Any]]:
        return [dict(row) for row in reversed(self._rows(
            "SELECT minute,ts,first,high,low,last,samples FROM opening_tape WHERE env=? AND inst=? ORDER BY minute DESC LIMIT ?",
            (env, inst, limit)))]

    def get(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def delete(self, key: str) -> None:
        self._exec("DELETE FROM kv WHERE key=?", (key,))

    # ----- journal -----
    def log(self, env: str, kind: str, title: str, body: dict[str, Any] | None = None, inst_id: str = "") -> dict[str, Any]:
        ts = now_ms()
        cur = self._exec(
            "INSERT INTO journal(ts,env,inst_id,kind,title,body) VALUES(?,?,?,?,?,?)",
            (ts, env, inst_id, kind, title, json.dumps(body or {}, ensure_ascii=False, default=str)),
        )
        return {"id": cur.lastrowid, "ts": ts, "env": env, "inst_id": inst_id, "kind": kind, "title": title, "body": body or {}}

    def journal(self, env: str, limit: int = 100, inst_id: str | None = None, before_id: int | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM journal WHERE env=?", [env]
        if inst_id:
            sql += " AND inst_id=?"
            params.append(inst_id)
        if before_id:
            sql += " AND id<?"
            params.append(before_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(min(max(limit, 1), 500))
        return [{**dict(r), "body": json.loads(r["body"])} for r in self._rows(sql, tuple(params))]

    # ----- pending -----
    def add_pending(self, env: str, action: dict[str, Any], preview: dict[str, Any] | None = None) -> int:
        cur = self._exec(
            "INSERT INTO pending(ts,env,action,preview) VALUES(?,?,?,?)",
            (now_ms(), env, json.dumps(action, ensure_ascii=False), json.dumps(preview or {}, ensure_ascii=False, default=str)),
        )
        return int(cur.lastrowid or 0)

    def pending(self, env: str, status: str | None = "waiting", limit: int = 50) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM pending WHERE env=?", [env]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [self._pending_row(r) for r in self._rows(sql, tuple(params))]

    def get_pending(self, pending_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM pending WHERE id=?", (pending_id,))
        return self._pending_row(rows[0]) if rows else None

    def claim_pending(self, pending_id: int) -> bool:
        """原子地把 waiting 改成 running，保证一条指令只会被执行一次。"""
        cur = self._exec("UPDATE pending SET status='running' WHERE id=? AND status='waiting'", (pending_id,))
        return cur.rowcount == 1

    def resolve_pending(self, pending_id: int, status: str, result: dict[str, Any]) -> None:
        self._exec(
            "UPDATE pending SET status=?, result=?, resolved_ts=? WHERE id=?",
            (status, json.dumps(result, ensure_ascii=False, default=str), now_ms(), pending_id),
        )

    def expire_pending(self, env: str, older_than_ms: int) -> int:
        cur = self._exec(
            "UPDATE pending SET status='expired', resolved_ts=? WHERE env=? AND status='waiting' AND ts<?",
            (now_ms(), env, now_ms() - older_than_ms),
        )
        return cur.rowcount

    @staticmethod
    def _pending_row(row: sqlite3.Row) -> dict[str, Any]:
        return {**dict(row), "action": json.loads(row["action"]), "result": json.loads(row["result"]), "preview": json.loads(row["preview"] or "{}")}

    # ----- equity -----
    # ----- 任务（work item）：每个触发事件对应一条，Worker 的状态都落在这里 -----
    def add_work(self, env: str, inst_id: str, source: str, goal: str, status: str = "queued", model: str = "") -> int:
        now = now_ms()
        cur = self._exec(
            "INSERT INTO work_items (ts, updated_ts, env, inst_id, source, goal, status, model) VALUES (?,?,?,?,?,?,?,?)",
            (now, now, env, inst_id, source, goal, status, model),
        )
        return int(cur.lastrowid)

    def update_work(self, work_id: int, status: str, result: str = "", model: str | None = None) -> None:
        if model is None:
            self._exec("UPDATE work_items SET status=?, result=?, updated_ts=? WHERE id=?", (status, result[:2000], now_ms(), work_id))
        else:
            self._exec("UPDATE work_items SET status=?, result=?, model=?, updated_ts=? WHERE id=?", (status, result[:2000], model, now_ms(), work_id))

    # ----- 策略：Agent 布置、执行器按规则自动执行的打法 -----
    @staticmethod
    def _strategy(row: Any) -> dict[str, Any]:
        return {**dict(row), "params": json.loads(row["params"] or "{}"), "state": json.loads(row["state"] or "{}")}

    def add_strategy(self, env: str, inst_id: str, kind: str, side: str, params: dict[str, Any], status: str = "armed", note: str = "") -> dict[str, Any]:
        now = now_ms()
        cur = self._exec(
            "INSERT INTO strategies (ts, updated_ts, env, inst_id, kind, side, status, params, note) VALUES (?,?,?,?,?,?,?,?,?)",
            (now, now, env, inst_id, kind, side, status, json.dumps(params, ensure_ascii=False), note),
        )
        return self.get_strategy(int(cur.lastrowid))  # type: ignore[return-value]

    def get_strategy(self, strategy_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM strategies WHERE id=?", (strategy_id,))
        return self._strategy(rows[0]) if rows else None

    def update_strategy(self, strategy_id: int, *, status: str | None = None, state: dict[str, Any] | None = None, note: str | None = None) -> None:
        sets, params = ["updated_ts=?"], [now_ms()]
        if status is not None:
            sets.append("status=?")
            params.append(status)
        if state is not None:
            sets.append("state=?")
            params.append(json.dumps(state, ensure_ascii=False))
        if note is not None:
            sets.append("note=?")
            params.append(note[:500])
        self._exec(f"UPDATE strategies SET {', '.join(sets)} WHERE id=?", (*params, strategy_id))

    def claim_demo_order(self, strategy_id: int) -> bool:
        cur = self._exec("INSERT OR IGNORE INTO kv (key,value) VALUES (?, 'true')", (f"strategy_order:demo:{strategy_id}",))
        return cur.rowcount == 1

    def strategies(self, env: str, inst_id: str | None = None, live: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM strategies WHERE env=?", [env]
        if inst_id:
            sql += " AND inst_id=?"
            params.append(inst_id)
        if live:
            sql += " AND status IN ('proposed','armed','active')"
        return [self._strategy(r) for r in self._rows(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))]

    def add_activity(self, env: str, inst_id: str, agent: str, kind: str, text: str, work_id: int | None = None, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """活动流里的一步：谁（router / worker / intel / smart / risk）在哪个币上做了什么。"""
        row = {"ts": now_ms(), "env": env, "inst_id": inst_id, "agent": agent, "work_id": work_id, "kind": kind, "text": text[:1200], "data": data or {}}
        cur = self._exec(
            "INSERT INTO activity (ts, env, inst_id, agent, work_id, kind, text, data) VALUES (?,?,?,?,?,?,?,?)",
            (row["ts"], env, inst_id, agent, work_id, kind, row["text"], json.dumps(row["data"], ensure_ascii=False, default=str)),
        )
        return {"id": int(cur.lastrowid), **row}

    def activity(self, env: str, limit: int = 200, inst_id: str | None = None, after_id: int = 0) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM activity WHERE env=? AND id>?", [env, after_id]
        if inst_id:
            sql += " AND inst_id=?"
            params.append(inst_id)
        rows = self._rows(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
        return [{**r, "data": json.loads(r["data"] or "{}")} for r in reversed(rows)]

    def work_items(self, env: str, limit: int = 60, inst_id: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM work_items WHERE env=?", [env]
        if inst_id:
            sql += " AND inst_id=?"
            params.append(inst_id)
        rows = self._rows(sql + " ORDER BY id DESC LIMIT ?", (*params, limit))
        return [dict(row) for row in rows]

    def work_detail(self, env: str, work_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM work_items WHERE env=? AND id=?", (env, work_id))
        if not rows:
            return None
        events = self._rows("SELECT * FROM activity WHERE env=? AND work_id=? ORDER BY id DESC LIMIT 200", (env, work_id))
        return {**dict(rows[0]), "activity": [{**r, "data": json.loads(r["data"] or "{}")} for r in reversed(events)]}

    def clear_history(self, env: str, inst_id: str, what: str) -> int:
        """清掉已经结束的记录，返回清了几条。inst_id 为空 = 这个环境里所有币。只清记录：进行中的任务、还在等的触发线和策略、持仓和挂单都不动。
        what: tasks 已完成 / 失败的任务；triggers 已结束的触发线和策略；talk 对话区的活动流（还没结束的任务的那几步留着）。"""
        where, params = "env=?" + (" AND inst_id=?" if inst_id else ""), [env, *([inst_id] if inst_id else [])]
        if what == "tasks":
            return self._exec(f"DELETE FROM work_items WHERE {where} AND status IN ('done','failed')", params).rowcount
        if what == "talk":
            return self._exec(
                f"DELETE FROM activity WHERE {where} AND (work_id IS NULL OR work_id NOT IN (SELECT id FROM work_items WHERE status IN ('queued','running','waiting')))", params
            ).rowcount
        if what == "triggers":
            gone = 0
            for row in self._rows(f"SELECT * FROM strategies WHERE {where} AND status NOT IN ('proposed','armed','active')", params):
                row = self._strategy(row)
                if row["params"].get("playbook") and row["state"].get("since") and not row["state"].get("scored"):
                    continue  # 用策略库做的单，战绩还没记：先留着
                gone += self._exec("DELETE FROM strategies WHERE id=?", (row["id"],)).rowcount
            return gone
        raise ValueError(what)

    def prune(self, keep_days: float) -> dict[str, int]:
        """删掉 keep_days 天之前的运行记录（所有环境）：活动流、复盘日志、已结束的任务和策略。
        不动的：进行中的任务、还在等的策略、仓位记录、逐笔复盘得到的经验、各打法的战绩、设置；权益曲线至少留 30 天。"""
        cut = now_ms() - int(keep_days * 86_400_000) + (1 if keep_days <= 0 else 0)  # 0 天 = 连这一毫秒刚写的也删
        live = "(SELECT id FROM work_items WHERE status IN ('queued','running','waiting'))"
        out = {
            "算法事件": self._exec("DELETE FROM algo_events WHERE ts < ?", (cut,)).rowcount,
            "开盘采样": self._exec("DELETE FROM opening_tape WHERE ts < ?", (cut,)).rowcount,
            "活动流": self._exec(f"DELETE FROM activity WHERE ts < ? AND (work_id IS NULL OR work_id NOT IN {live})", (cut,)).rowcount,
            "日志": self._exec("DELETE FROM journal WHERE ts < ? AND kind != 'review'", (cut,)).rowcount,
            "任务": self._exec("DELETE FROM work_items WHERE updated_ts < ? AND status IN ('done','failed')", (cut,)).rowcount,
            "已结束的策略": self._exec("DELETE FROM strategies WHERE updated_ts < ? AND status NOT IN ('proposed','armed','active')", (cut,)).rowcount,
            "权益曲线": self._exec("DELETE FROM equity WHERE ts < ?", (now_ms() - int(max(keep_days, 30) * 86_400_000),)).rowcount,
        }
        if sum(out.values()) > 500:  # 删得多才整理文件，把空间真正还给磁盘
            with self._lock:
                self._db.execute("VACUUM")
        return out

    def log_counts(self) -> dict[str, int]:
        return {name: self._rows(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"] for name, table in (("活动流", "activity"), ("日志", "journal"), ("任务", "work_items"), ("策略记录", "strategies"), ("开盘采样", "opening_tape"))}

    def fail_running_work(self, reason: str) -> int:
        """进程重启后，上次没跑完的任务不会再有结果，标成失败。"""
        return self._exec("UPDATE work_items SET status='failed', result=?, updated_ts=? WHERE status IN ('queued','running')", (reason, now_ms())).rowcount

    # ----- 每一笔仓位（lot）：欧易把同一个币同方向的仓位合并成一个，每笔的风格、入场价、止损止盈由我们自己记 -----
    def add_lot(self, env: str, inst_id: str, side: str, style: str, contracts: float, entry_px: float, *, stop_loss: float | None = None,
                take_profit: float | None = None, ord_id: str = "", source: str = "", status: str = "open", note: str = "") -> int:
        cur = self._exec(
            "INSERT INTO lots (ts, env, inst_id, side, style, contracts, entry_px, stop_loss, take_profit, ord_id, source, status, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_ms(), env, inst_id, side, style, contracts, entry_px, stop_loss, take_profit, ord_id, source, status, note),
        )
        return int(cur.lastrowid)

    def lots(self, env: str, inst_id: str | None = None, live: bool = True, limit: int = 200) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM lots WHERE env=?", [env]
        if inst_id:
            sql += " AND inst_id=?"
            params.append(inst_id)
        sql += " AND status IN ('open','working') ORDER BY id" if live else " ORDER BY id DESC LIMIT ?"
        return [dict(row) for row in self._rows(sql, tuple(params) if live else (*params, limit))]

    def lot(self, lot_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM lots WHERE id=?", (lot_id,))
        return dict(rows[0]) if rows else None

    def update_lot(self, lot_id: int, **fields: Any) -> None:
        if fields.get("status") == "closed":
            fields.setdefault("closed_ts", now_ms())
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE lots SET {cols} WHERE id=?", (*fields.values(), lot_id))

    def add_equity(self, env: str, equity: float) -> None:
        self._exec("INSERT INTO equity(ts,env,equity) VALUES(?,?,?)", (now_ms(), env, equity))

    def equity_curve(self, env: str, since_ms: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self._rows("SELECT ts,equity FROM equity WHERE env=? AND ts>=? ORDER BY ts", (env, since_ms))]

    def close(self) -> None:
        with self._lock:
            self._db.close()
