from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic_ai.models.function import FunctionModel

from ouy.app import create_app
from ouy.diagnostics import exception_detail, redact

from .test_agent_and_api import BTC, make_rt


def test_diagnostics_preserve_cause_and_request_id_without_credentials():
    class ProviderError(RuntimeError):
        status_code = 503
        model_name = "test-model"
        request_id = "request-abcdefghijklmnopqrstuvwxyz123456"

    try:
        try:
            raise ValueError("underlying response body")
        except ValueError as cause:
            raise ProviderError("Authorization: Bearer private-value; api_key='another-value' " + "x" * 2500) from cause
    except ProviderError as exc:
        detail = exception_detail(exc)
    raw = json.dumps(detail)
    assert "private-value" not in raw and "another-value" not in raw
    assert detail["status_code"] == "503" and detail["request_id"].startswith("request-")
    assert len(detail["message"]) > 2000
    assert detail["causes"][0]["type"] == "ValueError" and detail["frames"]
    assert "password" not in redact("https://name:password@example.com")
    assert "token-value" not in redact('"refresh_token": "token-value"')


async def test_failure_detail_is_persisted_and_scoped_to_environment(settings, fake):
    def fail(messages, info):
        raise RuntimeError("provider failure " + "long body " * 400 + "TAIL_EVIDENCE")

    rt = make_rt(settings, fake, FunctionModel(fail))
    await rt.watcher._review_task(BTC, "test")
    row = rt.store.work_items("demo")[0]
    assert len(row["result"]) <= 300 and row["status"] == "failed"
    assert rt.watcher.status()["instruments"][BTC]["error_work_id"] == row["id"]
    app = create_app(rt)
    app.state.rt = rt
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(f"/api/work/{row['id']}")
        assert response.status_code == 200
        failure = next(e for e in response.json()["activity"] if e["kind"] == "error")
        assert failure["data"]["error"]["message"].endswith("TAIL_EVIDENCE")
        assert failure["data"]["error"]["type"] == "RuntimeError"
        rt.save_config(rt.config().model_copy(update={"env": "live"}))
        assert (await client.get(f"/api/work/{row['id']}")).status_code == 404


def test_cancellation_does_not_claim_previous_orders_were_never_submitted():
    from ouy.agent.watcher import _fail_text

    kind, message = _fail_text(asyncio.CancelledError())
    assert kind == "skip" and "执行回执" in message and "没有因此下单" not in message


async def test_router_errors_remain_inspectable_after_reloading_chat(settings, fake):
    def fail(messages, info):
        raise RuntimeError("upstream 503 api_key='hidden-key' " + "details " * 100 + "END")

    rt = make_rt(settings, fake, FunctionModel(fail))
    with pytest.raises(RuntimeError):
        await rt.watcher.chat("check task state")
    history = rt.watcher.chat_log("general")
    assert history[-2]["role"] == "user" and history[-1]["error"]["message"].endswith("END")
    assert "hidden-key" not in json.dumps(history)
    assert rt.store.activity("demo")[-1]["kind"] == "error"


async def test_cancelled_queued_dispatch_is_not_left_running_forever(rt):
    from ouy.agent.core import Deps

    st = rt.watcher._state(BTC)
    parent = Deps(trader=rt.trader, market=rt.market, gateway=rt.gateway, config=rt.config(), kb=rt.kb, propose_only=True)
    await st.lock.acquire()
    job = asyncio.create_task(rt.watcher._dispatch(BTC, "queued test", parent))
    try:
        await asyncio.sleep(0)
        assert rt.store.work_items("demo")[0]["status"] == "queued"
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        row = rt.store.work_items("demo")[0]
        assert row["status"] == "failed" and st.waiting == 0
        assert rt.store.work_detail("demo", row["id"])["activity"][-1]["data"]["error"]["type"] == "CancelledError"
    finally:
        st.lock.release()
