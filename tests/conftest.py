from __future__ import annotations

import pytest

from ouy.app import Runtime
from ouy.config import Settings
from ouy.models import AgentConfig
from ouy.store import Store

from .fake_okx import FakeAuth, FakeOkx


@pytest.fixture(autouse=True)
def _no_saved_reports(tmp_path, monkeypatch):
    """测试不读本机 data/backtest 里的真实回测报告：有没有那几份文件不该改变测试结果。"""
    from ouy import backtest_plans

    monkeypatch.setattr(backtest_plans, "REPORT_DIR", tmp_path / "no-reports")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, OPENAI_API_KEY="test", LOCAL_PROXY_URL="", KB_EMBEDDING_MODEL="", OUY_DATA_DIR=str(tmp_path), OUY_SKILLS_DIR=str(tmp_path / "skills"))


@pytest.fixture
def fake() -> FakeOkx:
    return FakeOkx()


@pytest.fixture
def rt(settings, fake) -> Runtime:
    runtime = Runtime(settings, gateway=fake, auth=FakeAuth(fake), store=Store(":memory:"), model_factory=lambda: "test")
    runtime.save_config(AgentConfig(mode="auto", fast_entry="off"))
    return runtime
