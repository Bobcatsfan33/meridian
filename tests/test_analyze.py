"""Step 4: ad-hoc single-name analysis for an out-of-universe ticker (offline core)."""
from __future__ import annotations

import datetime as dt

from meridian.analyze import build_adhoc
from meridian.config import Config
from meridian.outputs.render import render_card

TARGET = dt.date(2026, 6, 26)


def _bars(base: float, n: int = 45, jump: float | None = None):
    bars = []
    for i in range(n):
        d = TARGET - dt.timedelta(days=(n - 1 - i))
        c = base + i * 0.05
        if i == n - 1 and jump is not None:
            c = c * (1 + jump)
        bars.append({"date": d, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                     "volume": 1_000_000 + i})
    return bars


def _window():
    # TSM is out-of-universe; give it an abnormal day-D move, plus a sector ETF + the market
    return {"TSM": _bars(100.0, jump=0.08), "XLK": _bars(200.0), "SPY": _bars(600.0)}


def test_build_adhoc_produces_labeled_card(tmp_path, monkeypatch):
    cfg = Config.load()
    # keep all artifacts under tmp (scratch DB + cache live under cfg.root/data)
    monkeypatch.setattr(cfg, "raw", {**cfg.raw}, raising=False)
    ev = build_adhoc(cfg, "TSM", TARGET, _window(), sector="Information Technology", sector_etf="XLK")

    assert ev["ticker"] == "TSM"
    assert ev["ad_hoc"] is True and ev["data_source"] == "ad_hoc"
    # a real pattern can fire on the abnormal move (options_led_proxy / sector_sympathy) OR,
    # if nothing qualifies, the honest ad-hoc no-explanation read — never a crash, never empty.
    assert ev["pattern"]["id"] in {"options_led_proxy", "sector_sympathy", "price_before_news", "none"}
    assert abs(sum(d["weight"] for d in ev["drivers"]) + ev["unexplained_residual"] - 1.0) < 1e-6

    out = render_card(ev)
    assert "TSM" in out
    assert "Ad-hoc — not part of the tracked universe" in out   # the ◆ banner
    assert "Not investment advice." in out


def test_build_adhoc_no_sector_skips_sector_pattern(tmp_path):
    cfg = Config.load()
    # no sector ETF -> sector_sympathy can't bind its sector leg; must not crash
    ev = build_adhoc(cfg, "ZZZZ", TARGET, {"ZZZZ": _bars(50.0, jump=0.05), "SPY": _bars(600.0)},
                     sector=None, sector_etf=None)
    assert ev["ticker"] == "ZZZZ" and ev["ad_hoc"] is True
    assert ev["pattern"]["id"] != "sector_sympathy"
    assert render_card(ev)
