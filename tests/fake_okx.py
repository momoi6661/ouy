"""测试用的假欧易网关：在内存里模拟一个只有 U 本位永续的交易所，接口形状与官方 MCP 返回一致。"""
from __future__ import annotations

import itertools
import math
from decimal import Decimal
import time
from typing import Any

from ouy.okx.gateway import OkxError, ToolInfo

READ_ONLY = {
    "account_get_config", "account_get_balance", "account_get_positions", "swap_get_orders", "swap_get_algo_orders",
    "swap_get_order", "swap_get_leverage", "swap_get_fills", "account_get_positions_history", "market_get_ticker", "market_get_tickers", "market_get_instruments",
    "market_get_candles", "market_get_orderbook", "market_get_funding_rate", "market_get_open_interest",
}
WRITE = {"swap_set_leverage", "swap_place_order", "swap_place_algo_order", "swap_amend_algo_order", "swap_cancel_algo_orders", "swap_close_position", "swap_cancel_order"}

SPECS = {
    "BTC-USDT-SWAP": {"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1", "lever": "100"},
    "ETH-USDT-SWAP": {"ctVal": "0.1", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.01", "lever": "100"},
}


class FakeOkx:
    def __init__(self, equity: float = 10_000.0):
        self.cash = equity
        self.prices = {"BTC-USDT-SWAP": 60_000.0, "ETH-USDT-SWAP": 3_000.0}
        self.leverage: dict[str, float] = {}
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: list[dict[str, Any]] = []
        self.receipts: dict[str, dict[str, Any]] = {}
        self.stuck: set[str] = set()  # 这些币没有对手盘：平仓单和限价单都挂着不成交
        self.limited = 0  # 接下来这么多次调用被限流
        self.algos: list[dict[str, Any]] = []
        self.algo_history: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.logged_in = True
        self.pos_mode = "net_mode"
        self.fail: dict[str, str] = {}
        self._ids = itertools.count(1000)

    # ----- Gateway 协议 -----
    async def tools(self) -> list[ToolInfo]:
        return [
            ToolInfo(name=n, module=n.split("_")[0], title=n, description=n, read_only=n in READ_ONLY, destructive=False,
                     schema={"type": "object", "properties": {"instId": {"type": "string"}}})
            for n in sorted(READ_ONLY | WRITE)
        ]

    async def restart(self) -> None: ...
    async def close(self) -> None: ...

    async def call(self, tool: str, args: dict[str, Any] | None = None, *, env: str) -> Any:
        args = {k: v for k, v in (args or {}).items() if v is not None and v != ""}
        self.calls.append((tool, args, env))
        if not tool.startswith("market_") and not self.logged_in:
            raise OkxError("No credentials found.", tool=tool, code="ConfigError")
        if self.limited > 0:
            self.limited -= 1
            raise OkxError("Too Many Requests（Rate limited. Back off and retry after a delay.）", tool=tool, code="429")
        if tool in self.fail:
            raise OkxError(self.fail[tool], tool=tool, code="51000")
        handler = getattr(self, f"_{tool}", None)
        if handler is None:
            raise OkxError(f"fake: unsupported tool {tool}", tool=tool)
        return handler(args)

    def called(self, tool: str) -> list[dict[str, Any]]:
        return [a for t, a, _ in self.calls if t == tool]

    # ----- 内部状态 -----
    def _upl(self, inst: str) -> float:
        p = self.positions[inst]
        return p["pos"] * float(SPECS[inst]["ctVal"]) * (self.prices[inst] - p["avgPx"])

    def equity(self) -> float:
        return self.cash + sum(self._upl(i) for i in self.positions)

    def set_price(self, inst: str, price: float) -> None:
        """改价并触发止盈止损。"""
        self.prices[inst] = price
        for algo in list(self.algos):
            if algo["instId"] != inst or inst not in self.positions:
                continue
            long = self.positions[inst]["pos"] > 0
            sl, tp = float(algo.get("slTriggerPx") or 0), float(algo.get("tpTriggerPx") or 0)
            hit = (sl and (price <= sl if long else price >= sl)) or (tp and (price >= tp if long else price <= tp))
            if not hit:
                continue
            oid = str(next(self._ids))
            self.algo_history.append({**algo, "state": "effective", "ordId": oid, "actualSide": "sl" if sl and (price <= sl if long else price >= sl) else "tp"})
            self.receipts[oid] = {"instId": inst, "ordId": oid, "side": algo["side"], "state": "filled",
                                  "accFillSz": str(min(float(algo.get("sz") or abs(self.positions[inst]["pos"])), abs(self.positions[inst]["pos"]))),
                                  "uTime": str(int(time.time() * 1000))}
            size = float(algo.get("sz") or 0)
            if algo.get("closeFraction") or not size or size >= abs(self.positions[inst]["pos"]) - 1e-9:
                self._flatten(inst)
            else:  # 按张数挂的止盈止损只平它自己那一笔
                self.algos.remove(algo)
                self._fill(inst, -size if long else size, price, self.positions[inst].get("mgnMode", "cross"))

    def _flatten(self, inst: str) -> None:
        pos = self.positions[inst]
        if abs(pos["pos"]) > 1e-9:
            self._record_close(inst, pos, self.prices[inst], self._upl(inst), "2")
        self.cash += self._upl(inst)
        del self.positions[inst]
        self.algos = [a for a in self.algos if a["instId"] != inst]

    def _record_close(self, inst: str, pos: dict, px: float, realized: float, kind: str) -> None:
        import time as _time

        self.history.append({
            "instId": inst, "direction": "long" if pos["pos"] > 0 else "short", "openAvgPx": str(pos["avgPx"]), "closeAvgPx": str(px),
            "realizedPnl": str(realized), "pnlRatio": "0", "lever": str(self.leverage.get(inst, 3)), "type": kind,
            "cTime": str(int(_time.time() * 1000) - 60_000), "uTime": str(int(_time.time() * 1000)),
        })

    def _account_get_positions_history(self, args: dict) -> list:
        rows = [h for h in self.history if not args.get("instId") or h["instId"] == args["instId"]]
        return rows[::-1][: int(args.get("limit") or 20)]

    def _fill(self, inst: str, signed: float, px: float, mgn: str = "cross") -> None:
        pos = self.positions.get(inst)
        if pos is None:
            self.positions[inst] = {"pos": signed, "avgPx": px, "cTime": str(next(self._ids)), "mgnMode": mgn}
            return
        if pos["pos"] * signed > 0:
            total = pos["pos"] + signed
            pos["avgPx"] = (pos["avgPx"] * pos["pos"] + px * signed) / total
            pos["pos"] = total
            return
        closing = min(abs(signed), abs(pos["pos"]))
        realized = closing * float(SPECS[inst]["ctVal"]) * (px - pos["avgPx"]) * (1 if pos["pos"] > 0 else -1)
        self._record_close(inst, pos, px, realized, "2" if closing >= abs(pos["pos"]) - 1e-9 else "1")
        self.cash += realized
        pos["pos"] += signed
        if abs(pos["pos"]) < 1e-9:
            self._flatten(inst)

    # ----- 账户 -----
    def _account_get_config(self, _: dict) -> list:
        return [{"acctLv": "2", "posMode": self.pos_mode}]

    def _account_get_balance(self, _: dict) -> list:
        eq = self.equity()
        used = sum(abs(p["pos"]) * float(SPECS[i]["ctVal"]) * self.prices[i] / self.leverage.get(i, 3) for i, p in self.positions.items())
        return [{"totalEq": str(eq), "details": [{"ccy": "USDT", "eq": str(eq), "availEq": str(eq - used)}]}]

    def _account_get_positions(self, _: dict) -> list:
        out = []
        for inst, p in self.positions.items():
            notional = abs(p["pos"]) * float(SPECS[inst]["ctVal"]) * self.prices[inst]
            lever = self.leverage.get(inst, 3)
            out.append({
                "instId": inst, "instType": "SWAP", "posSide": "net", "pos": str(p["pos"]), "avgPx": str(p["avgPx"]),
                "markPx": str(self.prices[inst]), "upl": str(self._upl(inst)), "uplRatio": str(self._upl(inst) / (notional / lever)),
                "lever": str(lever), "liqPx": "", "mgnMode": p["mgnMode"], "notionalUsd": str(notional), "cTime": p["cTime"],
            })
        return out

    def _swap_get_orders(self, args: dict) -> list:
        return [o for o in self.orders if not args.get("instId") or o["instId"] == args["instId"]]

    def _swap_get_order(self, args: dict) -> list:
        order = self.receipts.get(args["ordId"]) or next((item for item in self.orders if item["ordId"] == args["ordId"]), None)
        return [order] if order and order["instId"] == args["instId"] else []

    def _swap_get_algo_orders(self, args: dict) -> list:
        rows = self.algo_history if args.get("status") == "history" else self.algos
        return [a for a in rows if not args.get("instId") or a["instId"] == args["instId"]]

    def _swap_get_fills(self, _: dict) -> list:
        return []

    def _swap_get_leverage(self, args: dict) -> list:
        return [{"instId": args["instId"], "lever": str(self.leverage.get(args["instId"], 3)), "mgnMode": args["mgnMode"]}]

    def _swap_set_leverage(self, args: dict) -> list:
        self.leverage[args["instId"]] = float(args["lever"])
        return [{"instId": args["instId"], "lever": args["lever"]}]

    # ----- 下单 -----
    def _swap_place_order(self, args: dict) -> list:
        inst, sz = args["instId"], float(args["sz"])
        signed = sz if args["side"] == "buy" else -sz
        ord_id = str(next(self._ids))
        if args.get("reduceOnly") and inst not in self.positions:
            return [{"ordId": "", "sCode": "51169", "sMsg": "Order failed because you don't have any positions to reduce"}]
        if args["ordType"] == "fok":
            fill = inst not in self.stuck and (self.prices[inst] <= float(args["px"]) if signed > 0 else self.prices[inst] >= float(args["px"]))
            if fill:
                self._fill(inst, signed, self.prices[inst])
            self.receipts[ord_id] = {"instId": inst, "ordId": ord_id, "state": "filled" if fill else "canceled",
                                     "accFillSz": str(sz if fill else 0), "avgPx": str(self.prices[inst]) if fill else ""}
        elif args["ordType"] == "market":
            self._fill(inst, signed, self.prices[inst], args.get("tdMode", "cross"))
            if inst in self.positions and (args.get("slTriggerPx") or args.get("tpTriggerPx")):
                self.algos.append({
                    "algoId": str(next(self._ids)), "instId": inst, "ordType": "oco" if args.get("tpTriggerPx") else "conditional",
                    "side": "sell" if signed > 0 else "buy", "posSide": "net", "sz": args["sz"],
                    "slTriggerPx": args.get("slTriggerPx", ""), "tpTriggerPx": args.get("tpTriggerPx", ""),
                })
        else:
            self.orders.append({"ordId": ord_id, "instId": inst, "side": args["side"], "posSide": "net", "ordType": args["ordType"], "lever": str(self.leverage.get(inst, 3)),
                                "px": args["px"], "sz": args["sz"], "accFillSz": "0", "state": "live", "cTime": "1", "reduceOnly": "true" if args.get("reduceOnly") else "false"})
        return [{"ordId": ord_id, "sCode": "0", "sMsg": ""}]

    def _swap_place_algo_order(self, args: dict) -> list:
        if args.get("closeFraction") == "1" and any(a.get("closeFraction") == "1" and a["instId"] == args["instId"] for a in self.algos):
            raise OkxError("Only one full-position TP/SL", tool="swap_place_algo_order", code="51088")
        if args.get("sz") and Decimal(args["sz"]) % Decimal(SPECS[args["instId"]]["lotSz"]):
            raise OkxError("Order quantity must be a multiple of the lot size.", tool="swap_place_algo_order", code="51121")
        algo_id = str(next(self._ids))
        self.algos.append({
            "algoId": algo_id, "instId": args["instId"], "ordType": args["ordType"], "side": args["side"], "posSide": args.get("posSide", "net"),
            "sz": args.get("sz", ""), "closeFraction": args.get("closeFraction", ""), "slTriggerPx": args.get("slTriggerPx", ""), "tpTriggerPx": args.get("tpTriggerPx", ""),
        })
        return [{"algoId": algo_id, "sCode": "0", "sMsg": ""}]

    def _swap_cancel_algo_orders(self, args: dict) -> list:
        ids = {o["algoId"] for o in args["orders"]}
        self.algos = [a for a in self.algos if a["algoId"] not in ids]
        return [{"algoId": i, "sCode": "0"} for i in ids]

    def _swap_amend_algo_order(self, args: dict) -> list:
        row = next(a for a in self.algos if a["algoId"] == args["algoId"] and a["instId"] == args["instId"])
        for key in ("Sl", "Tp"):
            if f"new{key}TriggerPx" in args and args.get(f"new{key}TriggerPxType") not in ("last", "mark", "index"):
                raise OkxError(f"Parameter new{key}TriggerPxType error", tool="swap_amend_algo_order", code="51000")
        for key in ("Sl", "Tp"):
            if f"new{key}TriggerPx" in args:
                row[key.lower() + "TriggerPx"] = args[f"new{key}TriggerPx"]
                row[key.lower() + "TriggerPxType"] = args[f"new{key}TriggerPxType"]
        return [{"algoId": row["algoId"], "sCode": "0"}]

    def _swap_cancel_order(self, args: dict) -> list:
        for order in self.orders:
            if order["ordId"] == args["ordId"]:
                self.receipts[order["ordId"]] = {**order, "state": "canceled"}
        self.orders = [o for o in self.orders if o["ordId"] != args["ordId"]]
        return [{"ordId": args["ordId"], "sCode": "0"}]

    def _swap_close_position(self, args: dict) -> list:
        if args["instId"] not in self.positions:
            raise OkxError("Position does not exist", tool="swap_close_position", code="51023")
        if args["instId"] in self.stuck:  # 没有对手盘：平仓单挂着不成交
            pos = self.positions[args["instId"]]
            self.orders = [o for o in self.orders if not (o["instId"] == args["instId"] and o.get("reduceOnly") == "true")]
            self.orders.append({"ordId": str(next(self._ids)), "instId": args["instId"], "side": "buy" if pos["pos"] < 0 else "sell", "posSide": "net", "ordType": "market",
                                "lever": "3", "px": "", "sz": str(abs(pos["pos"])), "accFillSz": "0", "state": "live", "cTime": str(int(__import__("time").time() * 1000)), "reduceOnly": "true"})
            return [{"instId": args["instId"]}]
        self._flatten(args["instId"])
        return [{"instId": args["instId"]}]

    # ----- 行情 -----
    def _market_get_ticker(self, args: dict) -> list:
        px = self.prices[args["instId"]]
        return [{"instId": args["instId"], "last": str(px), "open24h": str(px * 0.99), "high24h": str(px * 1.02), "low24h": str(px * 0.97), "volCcy24h": "1000", "ts": "1700000000000"}]

    def _market_get_tickers(self, _: dict) -> list:
        return [self._market_get_ticker({"instId": i})[0] for i in self.prices]

    def _market_get_instruments(self, args: dict) -> list:
        ids = [args["instId"]] if args.get("instId") else list(SPECS)
        return [{"instId": i, "instType": "SWAP", "ctType": "linear", "settleCcy": "USDT", "state": "live", **SPECS[i]} for i in ids if i in SPECS]

    def _market_get_candles(self, args: dict) -> list:
        px, n = self.prices[args["instId"]], int(args.get("limit", 100))
        rows = []
        for i in range(n):  # 新的在前，和欧易一致；造一段缓慢上行并带波动的行情
            back = i
            close = px * (1 - 0.0004 * back) * (1 + 0.002 * math.sin(back / 3))
            rows.append([str(1_700_000_000_000 - back * 900_000), str(close * 0.999), str(close * 1.002), str(close * 0.997), str(close), "100", "1", "1", "0" if i == 0 else "1"])
        return rows

    def _market_get_orderbook(self, args: dict) -> list:
        px = self.prices[args["instId"]]
        return [{"asks": [[str(px + 0.1 * (i + 1)), "5", "0", "1"] for i in range(5)], "bids": [[str(px - 0.1 * (i + 1)), "6", "0", "1"] for i in range(5)]}]

    def _market_get_funding_rate(self, args: dict) -> list:
        return [{"instId": args["instId"], "fundingRate": "0.0001", "fundingTime": "1700003600000"}]

    def _market_get_open_interest(self, args: dict) -> list:
        return [{"instId": args.get("instId"), "oiUsd": "2000000000"}]


class FakeAuth:
    def __init__(self, fake: FakeOkx):
        self.fake = fake
        self.pending = False

    async def status(self) -> dict[str, Any]:
        if self.fake.logged_in:
            return {"status": "logged_in", "site": "global", "scopes": "fast_api"}
        if self.pending:
            return {"status": "pending", "site": "global", "verificationUri": "https://www.okx.com/account/oauth?flow=device", "userCode": "ABCD-EFGH", "ttl": 590}
        return {"status": "not_logged_in", "site": "global"}

    async def login(self) -> dict[str, Any]:
        self.pending = True
        return {"status": "pending", "site": "global", "verificationUri": "https://www.okx.com/account/oauth?flow=device", "userCode": "ABCD-EFGH", "expiresIn": 600}

    async def logout(self) -> None:
        self.fake.logged_in = False
        self.pending = False
