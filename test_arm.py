"""Arm preflight and server-clock safety checks."""
import datetime as dt
from email.utils import format_datetime

import httpx
import pytest
from fastapi import HTTPException

import config
import dashboard.app as dashboard
from kalshi.client import KalshiAPIError, KalshiClient


def test_client_records_date_skew_on_error():
    server_now = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=12)

    def handler(request):
        return httpx.Response(401, headers={"Date": format_datetime(server_now, usegmt=True)},
                              text="expired")

    client = KalshiClient.__new__(KalshiClient)
    client._key = client._key_id = None
    client.http = httpx.Client(base_url=config.KALSHI_BASE,
                               transport=httpx.MockTransport(handler))
    with pytest.raises(KalshiAPIError):
        client._req("GET", "/portfolio/balance")
    assert 10 <= client.clock_skew_s <= 13


@pytest.fixture
def arm_env(tmp_path, monkeypatch):
    now = 1_800_000_000.0
    state = tmp_path / "state"
    state.mkdir()
    pem = tmp_path / "key.pem"
    key_id = tmp_path / "key-id.txt"
    pem.write_text("present")
    key_id.write_text("present")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "ARMED_FILE", state / "ARMED")
    monkeypatch.setattr(config, "KILL_FILE", tmp_path / "KILL")
    monkeypatch.setattr(config, "KALSHI_PEM", pem)
    monkeypatch.setattr(config, "KALSHI_KEY_ID_FILE", key_id)
    monkeypatch.setattr(dashboard, "HALT_FILE", state / "HALT_UNTIL")
    monkeypatch.setattr(dashboard.time, "time", lambda: now)
    dashboard._last_status = {}

    status = {"ts": now - 1, "error": None, "scan_ok": True,
              "portfolio_sync_ok": True, "portfolio_sync_ts": now - 1,
              "clock_skew_s": 0.0, "balance": 25.29,
              "portfolio_value": 28.74}

    class GoodClient:
        clock_skew_s = 0.0

        def balance(self):
            return {"balance_dollars": "27.78"}

    class Con:
        def close(self):
            pass

    monkeypatch.setattr(dashboard, "KalshiClient", GoodClient)
    monkeypatch.setattr(dashboard.db, "connect", lambda: Con())
    monkeypatch.setattr(dashboard.db, "log_event", lambda *args: None)

    def save():
        import json
        (state / "status.json").write_text(json.dumps(status))

    save()
    return now, status, save


def test_arm_passes_complete_health_and_live_preflight(arm_env):
    assert dashboard.arm({"armed": True}) == {"armed": True}
    assert config.ARMED_FILE.read_text() == "0.00"


@pytest.mark.parametrize("change", [
    lambda s, n: s.update(ts=n - 301),
    lambda s, n: s.update(error="scan exploded"),
    lambda s, n: s.update(scan_ok=False),
    lambda s, n: s.update(portfolio_sync_ok=False),
    lambda s, n: s.update(portfolio_sync_ts=n - 301),
    lambda s, n: s.update(clock_skew_s=31),
    lambda s, n: s.pop("clock_skew_s"),
    lambda s, n: s.update(balance=None),
    lambda s, n: s.update(portfolio_value=float("nan")),
])
def test_arm_rejects_unhealthy_status(arm_env, change):
    now, status, save = arm_env
    change(status, now)
    save()
    with pytest.raises(HTTPException) as exc:
        dashboard.arm({"armed": True})
    assert exc.value.status_code == 409
    assert not config.ARMED_FILE.exists()


@pytest.mark.parametrize("sentinel", ["KILL", "HALT_UNTIL"])
def test_arm_rejects_kill_and_halt(arm_env, sentinel):
    now, _, _ = arm_env
    path = config.KILL_FILE if sentinel == "KILL" else dashboard.HALT_FILE
    path.write_text(str(now + 60) if sentinel == "HALT_UNTIL" else "")
    with pytest.raises(HTTPException) as exc:
        dashboard.arm({"armed": True})
    assert exc.value.status_code == 409
    assert not config.ARMED_FILE.exists()


def test_arm_cleans_expired_halt_and_rejects_malformed_halt(arm_env):
    now, _, _ = arm_env
    dashboard.HALT_FILE.write_text(str(now - 1))
    assert dashboard.arm({"armed": True}) == {"armed": True}
    assert not dashboard.HALT_FILE.exists()

    config.ARMED_FILE.unlink()
    dashboard.HALT_FILE.write_text("not-a-timestamp")
    with pytest.raises(HTTPException) as exc:
        dashboard.arm({"armed": True})
    assert exc.value.status_code == 409
    assert "invalid" in exc.value.detail
    assert not config.ARMED_FILE.exists()


def test_arm_requires_a_real_boolean(arm_env):
    with pytest.raises(HTTPException) as exc:
        dashboard.arm({"armed": "false"})
    assert exc.value.status_code == 422
    assert not config.ARMED_FILE.exists()


def test_arm_rejects_live_auth_or_clock_failure(arm_env, monkeypatch):
    class BadAuth:
        clock_skew_s = 0.0

        def balance(self):
            raise KalshiAPIError("401 expired", status=401)

    monkeypatch.setattr(dashboard, "KalshiClient", BadAuth)
    with pytest.raises(HTTPException) as exc:
        dashboard.arm({"armed": True})
    assert "balance preflight failed" in exc.value.detail
    assert not config.ARMED_FILE.exists()

    class BadClient:
        clock_skew_s = 45.0

        def balance(self):
            return {}

    monkeypatch.setattr(dashboard, "KalshiClient", BadClient)
    with pytest.raises(HTTPException) as exc:
        dashboard.arm({"armed": True})
    assert exc.value.status_code == 409
    assert "live system clock" in exc.value.detail
    assert not config.ARMED_FILE.exists()


def test_arm_rejects_missing_key(arm_env):
    config.KALSHI_PEM.unlink()
    with pytest.raises(HTTPException) as exc:
        dashboard.arm({"armed": True})
    assert exc.value.status_code == 409
    assert "keys are missing" in exc.value.detail
