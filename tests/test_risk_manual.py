"""用户自己在页面上下的单不过风控：只留不改就一定出错的几条。"""
import pytest

from ouy import risk
from ouy.models import AccountState, Instrument, Position, RiskLimits, TradeAction
from ouy.risk import Rejected

INST = Instrument(inst_id="BTC-USDT-SWAP", inst_type="SWAP", ct_type="linear", ct_val=0.01, tick_sz="0.1", lot_sz="0.01", min_sz="0.01", max_leverage=100, state="live")


def account(**kw):
    return AccountState(env="demo", equity=1000, available=1000, **kw)


def size(action, acc=None, manual=True, **kw):
    return risk.size_entry(action, account=acc or account(), inst=INST, limits=RiskLimits(), last_px=60000, current_leverage=10, manual=manual, **kw)


def test_manual_skips_risk_rules():
    near = TradeAction(kind="open", inst_id="BTC-USDT-SWAP", side="long", stop_loss=59990, margin_usdt=100, leverage=10)
    with pytest.raises(Rejected):
        size(near, manual=False)
    assert float(size(near).contracts) == pytest.approx(100 * 10 / (0.01 * 60000), abs=0.01)  # 照填的金额下，不缩小
    with pytest.raises(Rejected):
        size(near, account(halted=True), manual=False)
    assert size(near, account(halted=True)).contracts > 0
    wide = TradeAction(kind="open", inst_id="BTC-USDT-SWAP", side="long", stop_loss=54000, leverage=10, risk_pct=5)
    assert size(wide).risk_usd == pytest.approx(50, rel=0.05)  # 风险比例照填的 5%，不压到上限 1%


def test_manual_keeps_must_fail_checks():
    with pytest.raises(Rejected):  # 止损放错边
        size(TradeAction(kind="open", inst_id="BTC-USDT-SWAP", side="long", stop_loss=61000, margin_usdt=100))
    with pytest.raises(Rejected):  # 没有止损
        size(TradeAction(kind="open", inst_id="BTC-USDT-SWAP", side="long", margin_usdt=100))
    with pytest.raises(Rejected):  # 小于最小下单量
        size(TradeAction(kind="open", inst_id="BTC-USDT-SWAP", side="long", stop_loss=59000, margin_usdt=0.01, leverage=1))
    big = size(TradeAction(kind="open", inst_id="BTC-USDT-SWAP", side="long", stop_loss=59000, margin_usdt=5000, leverage=10))
    assert big.margin_usd <= 950 and any("可用保证金" in n for n in big.notes)  # 保证金不够：按可用的下
