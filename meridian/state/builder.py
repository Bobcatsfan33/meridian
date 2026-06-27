"""State builder (Phase 2). Derives rolling state + baselines from a PriceWindow and
writes ticker/sector/liquidity state, regimes_daily, and expected_behavior_1m.

No-lookahead: all rolling metrics for a date use only bars with date <= that date;
beta is estimated on returns strictly before the target date, then applied to the
target day's (same-day, not future) market return. Pure given the PriceWindow, so the
build is deterministic and golden-testable; only `prices.py` touches the network.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from . import baseline as bl
from . import regime as rg
from ..config import Config
from ..ingest.clock import market_close_utc
from .prices import PriceWindow

NAN = float("nan")


@dataclass
class StateSummary:
    target_date: dt.date
    n_symbols: int = 0
    n_ticker_state_rows: int = 0
    n_expected_behavior: int = 0
    n_sector_rows: int = 0
    n_liquidity_rows: int = 0
    regime_label: str = ""
    vix_level: float = NAN
    breadth: float = NAN
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Series:
    """Per-symbol aligned daily series (ascending)."""

    dates: list[dt.date]
    close: list[float]
    high: list[float]
    low: list[float]
    volume: list[float | None]
    ret: list[float]   # daily return, ret[i] vs close[i-1]; ret[0]=nan
    vwap: list[float]

    def upto(self, target: dt.date) -> int:
        """Index of the last date <= target, or -1."""
        idx = -1
        for i, d in enumerate(self.dates):
            if d <= target:
                idx = i
            else:
                break
        return idx

    def date_to_ret(self) -> dict[dt.date, float]:
        return {d: r for d, r in zip(self.dates, self.ret)}


def _series(bars: list[dict]) -> _Series:
    bars = sorted(bars, key=lambda b: b["date"])
    dates = [b["date"] for b in bars]
    close = [b["close"] for b in bars]
    high = [b.get("high") if b.get("high") is not None else b["close"] for b in bars]
    low = [b.get("low") if b.get("low") is not None else b["close"] for b in bars]
    volume = [b.get("volume") for b in bars]
    ret = [NAN] + bl.rolling_returns(close)
    vwap = [
        (h + lo + c) / 3 if None not in (h, lo, c) else c
        for h, lo, c in zip(high, low, close)
    ]
    return _Series(dates, close, high, low, volume, ret, vwap)


def build_state(
    con,
    cfg: Config,
    target_date: dt.date,
    price_window: PriceWindow,
    symbol_meta: dict[str, dict[str, Any]],
    index_symbol: str = "SPY",
    vix_symbol: str = "^VIX",
) -> StateSummary:
    f = cfg.featurization
    win = int(f.get("baseline_window_days", 60))
    rel_w = int(f.get("rel_volume_window_days", 20))
    atr_w = int(f.get("atr_window_days", 14))
    beta_w = int(f.get("beta_window_days", 60))
    sma_w = int(f.get("trend_sma_days", 50))

    series = {sym: _series(bars) for sym, bars in price_window.items() if bars}
    target_close_ts = market_close_utc(target_date).replace(tzinfo=None)

    # window of dates we persist into ticker_state_1m: <= target, last `win`+buffer
    _wipe_window(con, target_date, win + atr_w + 5)

    ticker_rows: list[tuple] = []
    liquidity_rows: list[tuple] = []
    n_symbols = 0
    for sym, s in series.items():
        end = s.upto(target_date)
        if end < 0:
            continue
        n_symbols += 1
        lo = max(0, end - (win + atr_w + 5) + 1)
        for i in range(lo, end + 1):
            ts = market_close_utc(s.dates[i]).replace(tzinfo=None)
            rel_vol = _rel_volume(s.volume, i, rel_w)
            atr = _atr(s.high, s.low, i, atr_w)
            ticker_rows.append((sym, ts, s.close[i], s.vwap[i], rel_vol, atr, s.ret[i]))
        # liquidity proxy for the target day only
        if s.dates[end] == target_date and symbol_meta.get(sym, {}).get("kind") == "stock":
            spread_bps = _spread_proxy(s.high[end], s.low[end], s.close[end])
            liquidity_rows.append((sym, target_close_ts, spread_bps, None, False))

    _insert_ticker_state(con, ticker_rows)
    _insert_liquidity(con, liquidity_rows)

    # --- expected-behavior (beta + macro) baseline for the target day ---
    market = series.get(index_symbol)
    mkt_ret_map = market.date_to_ret() if market else {}
    eb_rows = _expected_behavior(series, symbol_meta, target_date, mkt_ret_map, beta_w, target_close_ts)
    _replace_expected_behavior(con, target_date, eb_rows)

    # --- breadth + regime ---
    breadth = _breadth(series, symbol_meta, target_date)
    regime = _regime(cfg, series, target_date, vix_symbol, index_symbol, sma_w, win, breadth)
    _replace_regime(con, target_date, regime)

    # --- sector state ---
    sector_rows = _sector_state(series, symbol_meta, cfg, target_date, target_close_ts)
    _replace_sector_state(con, target_close_ts, sector_rows)

    return StateSummary(
        target_date=target_date,
        n_symbols=n_symbols,
        n_ticker_state_rows=len(ticker_rows),
        n_expected_behavior=len(eb_rows),
        n_sector_rows=len(sector_rows),
        n_liquidity_rows=len(liquidity_rows),
        regime_label=regime.regime_label,
        vix_level=regime.vix_level,
        breadth=breadth,
    )


# --- metric helpers --------------------------------------------------------------
def _rel_volume(volume: list, i: int, w: int) -> float:
    prior = [v for v in volume[max(0, i - w):i] if v is not None]
    if not prior or volume[i] is None:
        return NAN
    m = sum(prior) / len(prior)
    return volume[i] / m if m else NAN


def _atr(high: list, low: list, i: int, w: int) -> float:
    rng = [
        h - lo
        for h, lo in zip(high[max(0, i - w + 1): i + 1], low[max(0, i - w + 1): i + 1])
        if h is not None and lo is not None
    ]
    return sum(rng) / len(rng) if rng else NAN


def _spread_proxy(high: float, low: float, close: float) -> float:
    if None in (high, low, close) or not close:
        return NAN
    return (high - low) / close * 10000.0  # bps of intraday range (liquidity proxy)


def _expected_behavior(series, symbol_meta, target_date, mkt_ret_map, beta_w, ts) -> list[tuple]:
    rows: list[tuple] = []
    mkt_ret_today = mkt_ret_map.get(target_date, NAN)
    for sym, s in series.items():
        if symbol_meta.get(sym, {}).get("kind") != "stock":
            continue
        end = s.upto(target_date)
        if end < 0 or s.dates[end] != target_date:
            continue
        # returns strictly BEFORE target (no-lookahead beta estimation)
        stock_rets, mkt_rets = [], []
        for i in range(max(1, end - beta_w), end):  # dates < target
            d = s.dates[i]
            mr = mkt_ret_map.get(d, NAN)
            if not _nan(s.ret[i]) and not _nan(mr):
                stock_rets.append(s.ret[i])
                mkt_rets.append(mr)
        beta, alpha = bl.beta_alpha(stock_rets, mkt_rets)
        ret_today = s.ret[end]
        if _nan(beta) or _nan(mkt_ret_today):
            expected = NAN
            macro_c = NAN
            abnormal = NAN
        else:
            expected = alpha + beta * mkt_ret_today
            macro_c = beta * mkt_ret_today
            abnormal = (ret_today - expected) if not _nan(ret_today) else NAN
        rows.append((sym, ts, _null(expected), _null(beta), _null(macro_c), _null(abnormal)))
    return rows


def _breadth(series, symbol_meta, target_date) -> float:
    rets = []
    for sym, s in series.items():
        if symbol_meta.get(sym, {}).get("kind") != "stock":
            continue
        end = s.upto(target_date)
        if end >= 0 and s.dates[end] == target_date and not _nan(s.ret[end]):
            rets.append(s.ret[end])
    if not rets:
        return NAN
    return sum(1 for r in rets if r > 0) / len(rets)


def _regime(cfg, series, target_date, vix_symbol, index_symbol, sma_w, win, breadth) -> rg.Regime:
    f = cfg.featurization
    t = rg.RegimeThresholds(
        vix_high_pctile=float(f.get("vix_high_pctile", 0.70)),
        vix_low_pctile=float(f.get("vix_low_pctile", 0.30)),
        breadth_broad=float(f.get("breadth_broad", 0.60)),
        breadth_weak=float(f.get("breadth_weak", 0.40)),
    )
    vix = series.get(vix_symbol)
    vix_level = vix_pctile = vix_term = NAN
    if vix:
        e = vix.upto(target_date)
        if e >= 0:
            vix_level = vix.close[e]
            trailing = [c for c in vix.close[max(0, e - win):e]]  # dates < target
            vix_pctile = bl.percentile_rank(vix_level, trailing)
            m20 = bl.mean(vix.close[max(0, e - 20):e])
            vix_term = (vix_level / m20 - 1.0) if m20 and not _nan(m20) else NAN

    idx = series.get(index_symbol)
    idx_close = idx_sma = idx_sma_prev = NAN
    if idx:
        e = idx.upto(target_date)
        if e >= 0:
            idx_close = idx.close[e]
            idx_sma = bl.mean(idx.close[max(0, e - sma_w + 1): e + 1])
            idx_sma_prev = bl.mean(idx.close[max(0, e - sma_w): e]) if e >= 1 else NAN
    return rg.classify(
        vix_level=vix_level, vix_pctile=vix_pctile, vix_term=vix_term,
        index_close=idx_close, index_sma=idx_sma, index_sma_prev=idx_sma_prev,
        breadth=breadth, t=t,
    )


def _sector_state(series, symbol_meta, cfg, target_date, ts) -> list[tuple]:
    # map sector name -> sector ETF symbol (role=sector, description==sector)
    sector_to_etf: dict[str, str] = {}
    for sym, meta in symbol_meta.items():
        if meta.get("role") == "sector" and meta.get("sector_name"):
            sector_to_etf[meta["sector_name"]] = sym
    # breadth per sector from stocks
    sector_rets: dict[str, list[float]] = {}
    for sym, s in series.items():
        meta = symbol_meta.get(sym, {})
        if meta.get("kind") != "stock":
            continue
        sec = meta.get("sector")
        end = s.upto(target_date)
        if sec and end >= 0 and s.dates[end] == target_date and not _nan(s.ret[end]):
            sector_rets.setdefault(sec, []).append(s.ret[end])

    rows: list[tuple] = []
    for sec, etf in sector_to_etf.items():
        s = series.get(etf)
        etf_ret = NAN
        if s:
            e = s.upto(target_date)
            if e >= 0 and s.dates[e] == target_date:
                etf_ret = s.ret[e]
        rets = sector_rets.get(sec, [])
        breadth = (sum(1 for r in rets if r > 0) / len(rets)) if rets else NAN
        rows.append((sec, ts, etf, _null(etf_ret), _null(breadth)))
    return rows


# --- persistence (idempotent) ----------------------------------------------------
def _wipe_window(con, target_date, lookback_days: int) -> None:
    start = market_close_utc(target_date - dt.timedelta(days=int(lookback_days * 1.6) + 5)).replace(tzinfo=None)
    end = market_close_utc(target_date).replace(tzinfo=None)
    con.execute("DELETE FROM ticker_state_1m WHERE ts >= ? AND ts <= ?", [start, end])


def _insert_ticker_state(con, rows: list[tuple]) -> None:
    if rows:
        con.executemany(
            "INSERT INTO ticker_state_1m (ticker, ts, close, vwap, rel_volume, atr, ret_1m) "
            "VALUES (?,?,?,?,?,?,?)",
            [(a, b, c, d, _null(e), _null(g), _null(h)) for (a, b, c, d, e, g, h) in rows],
        )


def _insert_liquidity(con, rows: list[tuple]) -> None:
    con.execute("DELETE FROM liquidity_state_1m WHERE ts = ?",
                [rows[0][1]] if rows else [None])
    if rows:
        con.executemany(
            "INSERT INTO liquidity_state_1m (ticker, ts, spread_bps, depth, halted) VALUES (?,?,?,?,?)",
            [(a, b, _null(c), d, e) for (a, b, c, d, e) in rows],
        )


def _replace_expected_behavior(con, target_date, rows: list[tuple]) -> None:
    ts = market_close_utc(target_date).replace(tzinfo=None)
    con.execute("DELETE FROM expected_behavior_1m WHERE ts = ?", [ts])
    if rows:
        con.executemany(
            "INSERT INTO expected_behavior_1m (ticker, ts, expected_ret, beta, macro_component, abnormal_ret) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )


def _replace_regime(con, target_date, regime: rg.Regime) -> None:
    con.execute("DELETE FROM regimes_daily WHERE trade_date = ?", [target_date])
    con.execute(
        "INSERT INTO regimes_daily (trade_date, vix_level, vix_term, index_trend, breadth, "
        "regime_label, regime_tags) VALUES (?,?,?,?,?,?,?)",
        [target_date, _null(regime.vix_level), _null(regime.vix_term),
         regime.index_trend, _null(regime.breadth), regime.regime_label, list(regime.tags)],
    )


def _replace_sector_state(con, ts, rows: list[tuple]) -> None:
    con.execute("DELETE FROM sector_state_1m WHERE ts = ?", [ts])
    if rows:
        con.executemany(
            "INSERT INTO sector_state_1m (sector, ts, etf, etf_ret_1m, breadth) VALUES (?,?,?,?,?)",
            rows,
        )


def _nan(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _null(x):
    return None if _nan(x) else float(x)
