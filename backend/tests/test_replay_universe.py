from contextlib import nullcontext
from datetime import date

import pytest

from quantlab import replay_universe


def terms(code, day):
    return next(row for row in replay_universe.terms_on(date.fromisoformat(day)) if row["code"] == code)


def test_index_rename_and_etf_conversion_respect_effective_dates():
    assert terms("021964", "2026-09-19")["name"] == "天弘国证新能源电池指数发起C"
    assert terms("021964", "2026-09-21")["name"] == "天弘国证储能电池指数发起C"
    assert terms("025833", "2026-11-19")["name"] == "天弘中证电网设备主题指数发起C"
    assert terms("025833", "2026-11-20")["name"] == "天弘中证电网设备主题ETF发起联接C"


def test_disclosed_nav_precedes_software_fund_subscription():
    assert not terms("028521", "2026-08-20")["is_active"]
    assert not terms("028521", "2026-08-26")["is_active"]
    assert terms("028521", "2026-08-27")["is_active"]
    assert terms("028521", "2026-08-24")["evidence"] is None


def test_live_database_refused_before_any_reference_update(monkeypatch):
    monkeypatch.setattr(replay_universe.db, "transaction", nullcontext)
    monkeypatch.setattr(replay_universe.db, "query_one", lambda _: {"name": "quantlab"})
    monkeypatch.setattr(replay_universe.db, "execute_many", lambda *_: pytest.fail("unexpected write"))
    with pytest.raises(RuntimeError):
        replay_universe.apply_as_of(date(2026, 6, 10))
