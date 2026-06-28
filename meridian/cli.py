"""Meridian CLI (Phase 0). `meridian init` and `meridian status`."""
from __future__ import annotations
import typer
from rich.console import Console
from rich.table import Table
from .config import Config
from .storage import connect, init_db, table_counts

app = typer.Typer(add_completion=False, help="Meridian — local market event engine")
console = Console()


@app.command()
def init(config: str = typer.Option(None, help="Path to config.yaml")):
    """Create the DuckDB file, apply the schema, load the universe."""
    cfg = Config.load(config)
    res = init_db(cfg.duckdb_path, cfg.universe_file)
    console.print(f"[green]Initialized[/green] {cfg.duckdb_path}")
    console.print(f"  tables declared: {res['tables_declared']}")
    console.print(f"  universe rows:   {res['universe_rows']}")


@app.command()
def status(config: str = typer.Option(None, help="Path to config.yaml")):
    """Show DB path, run modes, and row counts per table."""
    cfg = Config.load(config)
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red] Run `meridian init` first.")
        raise typer.Exit(1)
    con = connect(cfg.duckdb_path)
    counts = table_counts(con)
    con.close()

    console.print(f"[bold]DB:[/bold] {cfg.duckdb_path}")
    rm = cfg.raw.get("run_modes", {})
    console.print(f"[bold]Run modes:[/bold] eod_batch="
                  f"{rm.get('eod_batch', {}).get('enabled')} "
                  f"intraday={rm.get('intraday', {}).get('enabled')}")
    console.print(f"[bold]Explanation:[/bold] "
                  f"{cfg.raw.get('explanation', {}).get('mode')}")

    t = Table("table", "rows")
    for name, n in counts.items():
        t.add_row(name, str(n))
    console.print(t)


@app.command()
def ingest(
    date: str = typer.Option(..., help="Trade date to ingest, YYYY-MM-DD"),
    adapter: list[str] = typer.Option(
        None, "--adapter", "-a",
        help="Force-enable specific adapter(s); repeatable. Default: config-enabled ones.",
    ),
    config: str = typer.Option(None, help="Path to config.yaml"),
    no_write: bool = typer.Option(False, help="Run adapters + audit but do not write to DuckDB"),
):
    """Ingest one historical day into normalized_events (dual timestamps)."""
    import datetime as _dt
    from .ingest import run_ingest
    from .ingest.clock import parse_date

    cfg = Config.load(config)
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red] Run `meridian init` first.")
        raise typer.Exit(1)
    try:
        trade_date = parse_date(date)
    except ValueError:
        console.print(f"[red]Bad --date {date!r}[/red]; expected YYYY-MM-DD.")
        raise typer.Exit(1)

    selected = adapter or None
    if selected is None and not _any_enabled(cfg):
        console.print("[yellow]No adapters enabled.[/yellow] Enable them in config.yaml "
                      "or pass --adapter NAME (e.g. -a yfinance -a fred -a edgar -a news_rss -a earnings).")
        raise typer.Exit(1)

    started = _dt.datetime.now()
    res = run_ingest(cfg, trade_date, selected=selected, write=not no_write)
    elapsed = (_dt.datetime.now() - started).total_seconds()

    console.print(f"[bold]Ingest {res.trade_date}[/bold]  universe={res.universe_size}  "
                  f"normalized_events=[green]{res.total_normalized}[/green]  ({elapsed:.1f}s)")

    fam = Table("event family", "count", title="Counts per event family")
    for name, n in res.family_counts.items():
        fam.add_row(name, str(n))
    if not res.family_counts:
        fam.add_row("(none)", "0")
    console.print(fam)

    ad = Table("adapter", "source", "fetched", "normalized", "failures", "status",
               title="Adapter coverage")
    for s in res.adapter_stats:
        ad.add_row(s.name, s.source, str(s.fetched), str(s.normalized),
                   ("[yellow]%d[/yellow]" % s.failures) if s.failures else "0",
                   "[red]" + s.error + "[/red]" if s.error else "[green]ok[/green]")
    console.print(ad)

    al = Table("source", "family", "rows", "min lat", "median lat", "max lat", "lookahead",
               title="Clock alignment (latency = ingest_time - event_time, seconds)")
    for a in res.alignment:
        al.add_row(a.source, a.family, str(a.count),
                   _fmt(a.min_latency_s), _fmt(a.median_latency_s), _fmt(a.max_latency_s),
                   ("[red]%d[/red]" % a.violations) if a.violations else "0")
    console.print(al)

    if res.lookahead_violations:
        console.print(f"[red]✗ {res.lookahead_violations} lookahead violation(s): "
                      f"ingest_time precedes event_time beyond clock-skew tolerance.[/red]")
        raise typer.Exit(2)
    console.print("[green]✓ clock alignment verified[/green] — no row received before it happened "
                  "(within feed latency tolerance).")


def _any_enabled(cfg: Config) -> bool:
    return any(b.get("enabled") for b in (cfg.raw.get("adapters", {}) or {}).values() if isinstance(b, dict))


def _fmt(x: float) -> str:
    import math
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if abs(x) >= 3600:
        return f"{x/3600:.1f}h"
    if abs(x) >= 60:
        return f"{x/60:.1f}m"
    return f"{x:.0f}s"


@app.command(name="ingest-intraday")
def ingest_intraday(
    date: str = typer.Option(..., help="Trade date, YYYY-MM-DD"),
    interval: str = typer.Option(None, help="Bar interval (default from config, e.g. 5m)"),
    ticker: list[str] = typer.Option(None, "--ticker", "-t", help="Symbols (repeatable)"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Ingest intraday bars into ticker_state_1m (ts = bar close UTC). Daily ingest unchanged."""
    from .state.intraday import run_intraday

    cfg = Config.load(config)
    target = _require_date(cfg, date)
    res = run_intraday(cfg, target, interval=interval, symbols=ticker or None)
    console.print(f"[bold]Intraday {res.target_date}[/bold]  interval={res.interval}  "
                  f"symbols={res.n_symbols}  ticker_state_rows=[green]{res.n_rows}[/green]")


@app.command()
def featurize(
    date: str = typer.Option(..., help="Trade date to featurize, YYYY-MM-DD"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Build rolling state + expected-behavior baseline, then grade events (L1)."""
    from .engine.featurize_run import run_featurize
    from .ingest.clock import parse_date

    cfg = Config.load(config)
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red] Run `meridian init` first.")
        raise typer.Exit(1)
    try:
        target = parse_date(date)
    except ValueError:
        console.print(f"[red]Bad --date {date!r}[/red]; expected YYYY-MM-DD.")
        raise typer.Exit(1)

    n_events = _count_day_events(cfg, target)
    if n_events == 0:
        console.print(f"[yellow]No normalized_events on {target}.[/yellow] Run "
                      f"`meridian ingest --date {target}` first.")
        raise typer.Exit(1)

    state, summ = run_featurize(cfg, target)

    console.print(f"[bold]Featurize {summ.target_date}[/bold]  "
                  f"events={summ.n_events}  graded=[green]{summ.n_graded}[/green]  "
                  f"insufficient_history={summ.n_insufficient_history}")
    console.print(f"[bold]Regime:[/bold] {summ.regime_label or '—'}  "
                  f"tags={summ.regime_tags}  vix={_num(state.vix_level)}  "
                  f"breadth={_num(state.breadth)}")
    console.print(f"[bold]State:[/bold] symbols={state.n_symbols}  "
                  f"ticker_state_rows={state.n_ticker_state_rows}  "
                  f"expected_behavior={state.n_expected_behavior}  "
                  f"sector_rows={state.n_sector_rows}  liquidity_rows={state.n_liquidity_rows}")

    t = Table("event family", "graded", "mean abnormality", title="L1 grading per family")
    abn = _abnormality_by_family(cfg, target)
    for fam, n in summ.family_counts.items():
        t.add_row(fam, str(n), _num(abn.get(fam, float("nan"))))
    console.print(t)
    console.print(f"[bold]Mean abnormality (all):[/bold] {_num(summ.abnormality_mean)}")
    if summ.n_graded == summ.n_events and summ.regime_label:
        console.print("[green]✓ every event graded with abnormality + regime_tags; "
                      "expected_behavior populated (residual denominator computable).[/green]")


def _count_day_events(cfg: Config, target) -> int:
    con = connect(cfg.duckdb_path)
    n = con.execute("SELECT count(*) FROM normalized_events WHERE CAST(event_time AS DATE)=?",
                    [target]).fetchone()[0]
    con.close()
    return n


def _abnormality_by_family(cfg: Config, target) -> dict:
    con = connect(cfg.duckdb_path)
    rows = con.execute(
        "SELECT n.family, avg(g.abnormality) FROM graded_events g "
        "JOIN normalized_events n USING(event_id) "
        "WHERE CAST(g.event_time AS DATE)=? GROUP BY n.family", [target]).fetchall()
    con.close()
    return {f: a for f, a in rows}


def _num(x) -> str:
    import math
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:.3f}"


@app.command()
def match(
    date: str = typer.Option(..., help="Trade date to match, YYYY-MM-DD"),
    patterns: str = typer.Option(None, help="Comma-separated pattern ids to run (default: all)"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Run L2 structural matching: graded events -> pattern firings + audited edges."""
    from .engine.match import run_match
    from .ingest.clock import parse_date

    cfg = Config.load(config)
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red] Run `meridian init` first.")
        raise typer.Exit(1)
    try:
        target = parse_date(date)
    except ValueError:
        console.print(f"[red]Bad --date {date!r}[/red]; expected YYYY-MM-DD.")
        raise typer.Exit(1)

    if _count_graded(cfg, target) == 0:
        console.print(f"[yellow]No graded_events on {target}.[/yellow] Run "
                      f"`meridian featurize --date {target}` first.")
        raise typer.Exit(1)

    pattern_ids = [p.strip() for p in patterns.split(",")] if patterns else None
    res = run_match(cfg, target, pattern_ids=pattern_ids)
    console.print(f"[bold]Match {res.target_date}[/bold]  targets={res.n_targets}  "
                  f"firings=[green]{res.n_firings}[/green]  edges={res.n_edges}  "
                  f"mean_completeness={_num(res.mean_completeness)}")

    p = Table("pattern", "firings", title="Firings per pattern")
    for name, n in res.per_pattern.items():
        p.add_row(name, str(n))
    if not res.per_pattern:
        p.add_row("(none)", "0")
    console.print(p)

    t = Table("ticker", "pattern", "completeness", title="Top firings")
    for ticker, rule_id, comp in res.top:
        t.add_row(ticker, rule_id, _num(comp))
    if res.top:
        console.print(t)
    console.print("[green]✓ patterns scored with graded completeness; edges audited with "
                  "rule_id (precedes downgraded until causal gate, Phase 6).[/green]")


def _count_graded(cfg: Config, target) -> int:
    con = connect(cfg.duckdb_path)
    n = con.execute("SELECT count(*) FROM graded_events WHERE CAST(event_time AS DATE)=?",
                    [target]).fetchone()[0]
    con.close()
    return n


@app.command()
def postmortem(
    date: str = typer.Option(..., help="Trade date, YYYY-MM-DD"),
    config: str = typer.Option(None, help="Path to config.yaml"),
    scanner_top: int = typer.Option(20, help="How many scanner rows to show"),
):
    """Build explanations, then print the ranked scanner + EOD postmortem."""
    from .outputs.build import build_explanations
    from .outputs.postmortem import build_context
    from .outputs.render import render_postmortem, render_scanner

    cfg = Config.load(config)
    target = _require_date(cfg, date)
    if _count_firings(cfg, target) == 0:
        console.print(f"[yellow]No pattern_firings on {target}.[/yellow] Run the pipeline: "
                      f"ingest → featurize → match for {target} first.")
        raise typer.Exit(1)

    evidences = build_explanations(cfg, target)
    top = sorted(evidences, key=lambda e: e["confidence"]["value"], reverse=True)[:scanner_top]
    console.print(render_scanner(top, target.isoformat()))
    console.print(render_postmortem(build_context(cfg, target, evidences)))
    bad = [e for e in evidences if e["unexplained_residual"] <= 0 or not e["invalidation"]]
    if bad:
        console.print(f"[red]✗ {len(bad)} card(s) missing residual/invalidation[/red]")
        raise typer.Exit(2)
    console.print(f"[green]✓ {len(evidences)} cards, each with unexplained residual + "
                  f"invalidation; attribution+residual=100% (enforced).[/green]")


@app.command()
def scanner(
    date: str = typer.Option(..., help="Trade date, YYYY-MM-DD"),
    config: str = typer.Option(None, help="Path to config.yaml"),
    top: int = typer.Option(40, help="How many rows"),
):
    """Print the ranked pattern scanner for a date."""
    from .outputs.build import build_explanations
    from .outputs.render import render_scanner

    cfg = Config.load(config)
    target = _require_date(cfg, date)
    evidences = build_explanations(cfg, target)
    rows = sorted(evidences, key=lambda e: e["confidence"]["value"], reverse=True)[:top]
    console.print(render_scanner(rows, target.isoformat()))


@app.command()
def card(
    ticker: str = typer.Option(..., help="Ticker symbol"),
    date: str = typer.Option(..., help="Trade date, YYYY-MM-DD"),
    pattern: str = typer.Option(None, help="Render this pattern's firing (default: best)"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Render the deterministic 'why is it moving' card for one ticker."""
    from .outputs.build import build_explanations
    from .outputs.render import render_card

    cfg = Config.load(config)
    target = _require_date(cfg, date)
    evidences = build_explanations(cfg, target, pattern_id=pattern)
    match = [e for e in evidences if e["ticker"] == ticker.upper()]
    if not match:
        console.print(f"[yellow]No explanation for {ticker.upper()} on {target}.[/yellow]")
        raise typer.Exit(1)
    console.print(render_card(match[0]))


def _require_date(cfg: Config, date: str):
    from .ingest.clock import parse_date
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red] Run `meridian init` first.")
        raise typer.Exit(1)
    try:
        return parse_date(date)
    except ValueError:
        console.print(f"[red]Bad --date {date!r}[/red]; expected YYYY-MM-DD.")
        raise typer.Exit(1)


def _count_firings(cfg: Config, target) -> int:
    con = connect(cfg.duckdb_path)
    n = con.execute("SELECT count(*) FROM pattern_firings WHERE CAST(window_start AS DATE)=?",
                    [target]).fetchone()[0]
    con.close()
    return n


@app.command()
def options(
    date: str = typer.Option(..., help="Trade date, YYYY-MM-DD"),
    ticker: list[str] = typer.Option(None, "--ticker", "-t", help="Limit to tickers (repeatable)"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Ingest option-chain snapshots -> GEX surface + dealer-positioning events (Phase 5)."""
    from .options.ingest import run_options

    cfg = Config.load(config)
    target = _require_date(cfg, date)
    res = run_options(cfg, target, tickers=ticker or None)
    console.print(f"[bold]Options {res.target_date}[/bold]  tickers={res.n_tickers}  "
                  f"dealer_pos events=[green]{res.n_events}[/green]  gex_surface_rows={res.n_surface_rows}")
    t = Table("event_type", "count", title="Dealer-positioning events")
    for k, n in sorted(res.event_type_counts.items()):
        t.add_row(k, str(n))
    if not res.event_type_counts:
        t.add_row("(none)", "0")
    console.print(t)
    if res.n_tickers == 0:
        console.print("[yellow]No chains found.[/yellow] Add fixtures under "
                      "config/fixtures/options/<date>/<TICKER>.json or enable Robinhood source.")
    else:
        console.print(f"[green]✓ run[/green] meridian featurize --date {target} && "
                      f"meridian match --patterns gamma_squeeze --date {target}")


@app.command()
def label(
    start: str = typer.Option(..., help="First firing date, YYYY-MM-DD"),
    end: str = typer.Option(..., help="Last firing date, YYYY-MM-DD"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Label forward returns (+MFE/MAE) for firings in a date range (Phase 6)."""
    from .predict.label import label_date_range

    cfg = Config.load(config)
    s, e = _require_date(cfg, start), _require_date(cfg, end)
    outcomes = label_date_range(cfg, s, e)
    console.print(f"[green]✓ labeled[/green] {len(outcomes)} pattern-outcomes "
                  f"({s}→{e}) into historical_pattern_outcomes.")


@app.command()
def calibrate(
    pattern: str = typer.Option(None, help="Pattern id (default: all)"),
    horizon: str = typer.Option("+1d", help="Forward horizon, e.g. +1d/+3d/+5d"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Walk-forward reliability curves (predicted vs realized) per pattern."""
    from .predict.calibrate import calibrate as run_cal

    cfg = Config.load(config)
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red]")
        raise typer.Exit(1)
    results = run_cal(cfg, pattern_id=pattern, horizon=horizon)
    if not results:
        console.print("[yellow]Not enough labeled outcomes to calibrate.[/yellow] "
                      "Run `meridian label` over a history first.")
        raise typer.Exit(1)
    for r in results:
        kind = "walk-forward" if r.walk_forward else "in-sample (insufficient history)"
        console.print(f"\n[bold]{r.pattern_id}[/bold]  horizon={r.horizon}  "
                      f"Brier={_num(r.brier)}  [{kind}]")
        t = Table("pred bin", "mean predicted", "realized hit-rate", "n", title="Reliability")
        for b in r.bins:
            t.add_row(f"{b.bin_lo:.2f}-{b.bin_hi:.2f}", _num(b.predicted), _num(b.realized), str(b.n))
        console.print(t)


@app.command()
def backtest(
    pattern: str = typer.Option(..., help="Pattern id to backtest"),
    horizon: str = typer.Option(None, help="Exit horizon (default from config)"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Honest paper backtest + forward odds/hit-rate/decay/reliability (Phase 6)."""
    from .predict.backtest import backtest as run_bt
    from .predict.forward import build_profile
    from .predict.label import label_date_range

    cfg = Config.load(config)
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red]")
        raise typer.Exit(1)

    # auto-label across the full firing-date range if outcomes are missing
    con = connect(cfg.duckdb_path)
    have = con.execute("SELECT count(*) FROM historical_pattern_outcomes WHERE pattern_id=?",
                       [pattern]).fetchone()[0]
    rng = con.execute("SELECT min(CAST(window_start AS DATE)), max(CAST(window_start AS DATE)) "
                      "FROM pattern_firings WHERE pattern_id=?", [pattern]).fetchone()
    con.close()
    if not rng or rng[0] is None:
        console.print(f"[yellow]No firings for {pattern}.[/yellow] Run match first.")
        raise typer.Exit(1)
    if have == 0:
        console.print(f"Labeling forward returns for {pattern} ({rng[0]}→{rng[1]})…")
        label_date_range(cfg, rng[0], rng[1])

    res = run_bt(cfg, pattern, horizon=horizon)
    if res.n_trades == 0:
        console.print(f"[yellow]No resolvable forward outcomes for {pattern}[/yellow] "
                      "(firings may be too recent to have forward data).")
        raise typer.Exit(1)

    console.print(f"[bold]Backtest {pattern}[/bold]  exit={res.horizon}  trades={res.n_trades}")
    console.print(f"  win-rate=[green]{_num(res.win_rate)}[/green]  mean_return={_num(res.mean_return)}  "
                  f"median={_num(res.median_return)}  MFE={_num(res.mean_mfe)}  MAE={_num(res.mean_mae)}")
    console.print(f"  [bold]honest residual attached:[/bold] mean unexplained "
                  f"residual={_num(res.mean_residual)} (the move share patterns could not attribute)")

    # forward odds + hit-rate + decay across horizons
    from .predict.label import Outcome as _O
    con = connect(cfg.duckdb_path)
    rows = con.execute("SELECT firing_id, pattern_id, regime_label, horizon, fwd_return, mfe, mae "
                       "FROM historical_pattern_outcomes WHERE pattern_id=?", [pattern]).fetchall()
    con.close()
    outcomes = [_O(*r) for r in rows]
    prof = build_profile(outcomes, pattern, regime_label=None,
                         threshold=float(cfg.predict.get("return_threshold", 0.0)))
    t = Table("horizon", "n", "hit-rate", "median ret", "P(>thr)", "mean MFE", "mean MAE",
              title="Forward odds (conditional — not a guarantee)")
    for h in prof.horizons:
        t.add_row(h.horizon, str(h.n), _num(h.hit_rate), _num(h.median_return),
                  _num(h.p_gt_threshold), _num(h.mean_mfe), _num(h.mean_mae))
    console.print(t)
    console.print("[bold]Decay (hit-rate by horizon):[/bold] " +
                  "  ".join(f"{h}={_num(v)}" for h, v in prof.decay))
    console.print("[dim]Not investment advice. Odds are conditional on pattern+regime and "
                  "carry the unexplained residual above.[/dim]")


@app.command(name="causal-test")
def causal_test(
    date: str = typer.Option(..., help="Date whose edges to test, YYYY-MM-DD"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Granger-gate event edges: upgrade to trusted `precedes` only if p < alpha (Phase 6)."""
    from .predict.causal_run import run_causal_tests

    cfg = Config.load(config)
    target = _require_date(cfg, date)
    res = run_causal_tests(cfg, target)
    console.print(f"[bold]Causal test {res.target_date}[/bold]  edges={res.n_edges}  "
                  f"tested={res.n_tested}  trusted_precedes=[green]{res.n_precedes}[/green]  "
                  f"(alpha={cfg.causal_test_alpha})")
    console.print("[green]✓ edges carry test_stat + test_pvalue; precedes only where "
                  "p < alpha (else downgraded).[/green]")


@app.command()
def backfill(
    start: str = typer.Option(..., help="First date, YYYY-MM-DD"),
    end: str = typer.Option(..., help="Last date, YYYY-MM-DD"),
    adapter: list[str] = typer.Option(None, "--adapter", "-a", help="Adapters (repeatable)"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Run the EOD batch across a date range to accumulate firing history (Phase 6)."""
    import datetime as _dt
    from .schedule.jobs import run_postclose

    cfg = Config.load(config)
    s, e = _require_date(cfg, start), _require_date(cfg, end)
    d = s
    total = 0
    while d <= e:
        if d.weekday() < 5:  # skip weekends (holidays yield no bars, handled downstream)
            try:
                res = run_postclose(cfg, d, adapters=adapter or None)
                total += res.firings
                console.print(f"  {d}: normalized={res.normalized} firings={res.firings}")
            except Exception as exc:
                console.print(f"  {d}: [red]{type(exc).__name__}: {exc}[/red]")
        d += _dt.timedelta(days=1)
    console.print(f"[green]✓ backfill {s}→{e} complete[/green]  total firings={total}")


@app.command(name="run-day")
def run_day(
    date: str = typer.Option(..., help="Trade date, YYYY-MM-DD"),
    adapter: list[str] = typer.Option(None, "--adapter", "-a", help="Adapters (repeatable)"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Run the full EOD batch for one day: ingest → featurize → match → explanations."""
    from .schedule.jobs import run_postclose

    cfg = Config.load(config)
    target = _require_date(cfg, date)
    res = run_postclose(cfg, target, adapters=adapter or None)
    console.print(f"[bold]Day run {res.target_date}[/bold]  steps={'→'.join(res.steps)}")
    console.print(f"  normalized={res.normalized}  graded={res.graded}  firings={res.firings}  "
                  f"options={res.options_events}  flow_firings={res.flow_firings}  "
                  f"explanations=[green]{res.explanations}[/green]  labeled={res.labeled}")
    console.print(f"[green]✓ done — view with[/green] meridian postmortem --date {target}")


@app.command(name="data-report")
def data_report(config: str = typer.Option(None, help="Path to config.yaml")):
    """Show rows/day by family + source, firings/pattern, outcome samples, and feed health."""
    from .reporting import build_data_report

    cfg = Config.load(config)
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red]")
        raise typer.Exit(1)
    rep = build_data_report(cfg)

    t = Table("family", "rows", "last event date", title="normalized_events by family")
    for fam, n, last in rep.by_family:
        t.add_row(fam, str(n), str(last))
    console.print(t)

    s = Table("source", "rows", "last ingest", title="by source")
    for src, n, last in rep.by_source:
        s.add_row(src, str(n), str(last))
    console.print(s)

    f = Table("pattern", "firings", title="pattern firings")
    for pid, n in rep.firings:
        f.add_row(pid, str(n))
    console.print(f)

    o = Table("pattern", "regime", "outcomes", title="outcome sample size (pattern × regime)")
    for pid, regime, n in rep.outcomes[:30]:
        o.add_row(pid, regime or "—", str(n))
    if rep.outcomes:
        console.print(o)

    h = Table("feed", "enabled", "detail", title="feed health")
    for row in rep.feeds:
        detail = ""
        if row["feed"] == "massive":
            detail = (f"key={row.get('key_present')} breaker={row.get('breaker')} "
                      f"throttle_wait={row.get('throttle_wait_s')}s")
        h.add_row(row["feed"], str(row["enabled"]), detail)
    console.print(h)


@app.command()
def backup(
    retain: int = typer.Option(14, help="How many daily backups to keep"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Copy data/meridian.duckdb to data/backups/meridian-YYYYMMDD.duckdb (retain N)."""
    from .schedule.jobs import backup_db

    cfg = Config.load(config)
    dest = backup_db(cfg, retain=retain)
    if dest:
        console.print(f"[green]✓ backup[/green] {dest}")
    else:
        console.print("[yellow]No DB to back up.[/yellow]")


@app.command()
def relearn(config: str = typer.Option(None, help="Path to config.yaml")):
    """Weekly self-improvement: refresh outcomes + calibration over all history; report gates."""
    from .predict.relearn import relearn as run_relearn

    cfg = Config.load(config)
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red]")
        raise typer.Exit(1)
    rep = run_relearn(cfg)
    console.print(f"[bold]Relearn[/bold] {rep.start or '—'} → {rep.end or '—'}")
    console.print(f"  outcomes: {rep.outcomes_before} → [green]{rep.outcomes_after}[/green]")
    console.print(f"  calibrated patterns: {rep.calibrated_patterns or '—'}")
    if rep.gates_opened:
        console.print(f"  [green]gates opened:[/green] {rep.gates_opened}")
    for note in rep.notes:
        console.print(f"  [dim]{note}[/dim]")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host (local only by default)"),
    port: int = typer.Option(8765, help="Port"),
    db: str = typer.Option(None, help="DB path to serve (default: config storage.duckdb_path)"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Serve the local dashboard (cards, scanner, postmortem) at http://host:port/."""
    import uvicorn

    from .api import create_app

    cfg = Config.load(config)
    if db:
        cfg.raw.setdefault("storage", {})["duckdb_path"] = db
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red] Run `meridian init` first.")
        raise typer.Exit(1)
    console.print(f"[bold]Meridian dashboard[/bold] → http://{host}:{port}/  (Ctrl+C to stop)")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")


@app.command()
def schedule(
    mode: str = typer.Option("postclose", help="postclose | premarket | intraday | both"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Start the APScheduler loop (blocking): pre-market, intraday, and/or post-close."""
    from .schedule.scheduler import build_scheduler

    cfg = Config.load(config)
    if not cfg.duckdb_path.exists():
        console.print("[red]DB not found.[/red] Run `meridian init` first.")
        raise typer.Exit(1)
    sched = build_scheduler(cfg, mode)
    jobs = ", ".join(f"{j.name} [{j.id}]" for j in sched.get_jobs())
    console.print(f"[bold]Scheduler started[/bold] (mode={mode}, tz=America/New_York). Jobs: {jobs}")
    console.print("Ctrl+C to stop.")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        console.print("Scheduler stopped.")


@app.command(name="install-daily")
def install_daily(
    activate: bool = typer.Option(False, help="Also load the launchd agent now (macOS)"),
    config: str = typer.Option(None, help="Path to config.yaml"),
):
    """Install scheduled local daily runs (launchd on macOS, cron elsewhere)."""
    from .schedule.install import install_daily as do_install

    cfg = Config.load(config)
    if not cfg.duckdb_path.exists():
        console.print("[yellow]Tip:[/yellow] run `meridian init` before the first scheduled run.")
    plan = do_install(cfg, activate=activate)
    if plan.path:
        console.print(f"[green]✓ wrote[/green] {plan.path}")
        console.print(f"  {plan.notes}")
        if activate:
            console.print("[green]✓ loaded[/green] launchd agent (running now).")
        else:
            console.print(f"  Activate with: [bold]{plan.activate_cmd}[/bold]  "
                          f"(or re-run with --activate)")
    else:
        console.print(f"[bold]Add these crontab lines[/bold] ({plan.notes}):\n{plan.content}")
        console.print(f"  {plan.activate_cmd}")


@app.command()
def demo(
    db: str = typer.Option(None, help="Sample DB path (default: data/demo.duckdb)"),
):
    """Offline, deterministic end-to-end demo on a tiny committed fixture (no keys, no network)."""
    from .demo import run_demo

    res = run_demo(db_path=db)
    console.print(f"[bold]Meridian demo[/bold]  date={res.date}  db={res.db_path}")
    console.print(f"  steps={'→'.join(res.steps)}")
    console.print(f"  events={res.n_events}  firings={res.n_firings}  "
                  f"cards=[green]{res.n_cards}[/green]")
    if res.top:
        t = Table("ticker", "pattern", "confidence", title="Top cards")
        for ticker, pat, conf in res.top:
            t.add_row(ticker, pat, _num(conf))
        console.print(t)
    if res.n_cards < 1:
        console.print("[red]✗ demo produced no cards.[/red]")
        raise typer.Exit(1)

    # show a real card inline so the user sees output in this one command
    from .demo import DEMO_DATE
    from .outputs.build import build_explanations
    from .outputs.render import render_card

    cfg = Config.load()
    cfg.raw.setdefault("storage", {})["duckdb_path"] = res.db_path
    evs = build_explanations(cfg, DEMO_DATE)
    if evs:
        top = max(evs, key=lambda e: e["confidence"]["value"])
        console.print("\n[bold]Sample card:[/bold]")
        console.print(render_card(top))
    console.print("[green]✓ demo complete (offline, no keys).[/green] View the full dashboard:")
    console.print(f"    meridian serve --db {res.db_path}   →   open http://127.0.0.1:8765/")


@app.command()
def version():
    from . import __version__
    console.print(__version__)


if __name__ == "__main__":
    app()
