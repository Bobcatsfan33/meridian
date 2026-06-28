"""FastAPI app (Phase 7). Read-only JSON endpoints + a single-page dashboard.

Cards/postmortem are rendered by the SAME deterministic Jinja renderers used by the
CLI, so the browser view is byte-identical to the terminal — no LLM, no divergence.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib

from ..config import Config
from ..storage import connect

_STATIC = pathlib.Path(__file__).with_name("static")


def create_app(cfg: Config | None = None):
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

    cfg = cfg or Config.load()
    app = FastAPI(title="Meridian", docs_url="/docs")

    def _con():
        if not cfg.duckdb_path.exists():
            raise HTTPException(503, "DB not found; run `meridian init`.")
        return connect(cfg.duckdb_path)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (_STATIC / "dashboard.html").read_text()

    @app.get("/api/dates")
    def dates():
        con = _con()
        try:
            rows = con.execute(
                "SELECT DISTINCT CAST(window_start AS DATE) d FROM move_explanations ORDER BY d DESC"
            ).fetchall()
        finally:
            con.close()
        return JSONResponse([str(r[0]) for r in rows])

    @app.get("/api/scanner")
    def scanner(date: str):
        d = _parse(date)
        con = _con()
        try:
            rows = con.execute(
                "SELECT evidence_object FROM move_explanations WHERE CAST(window_start AS DATE)=?",
                [d]).fetchall()
        finally:
            con.close()
        out = []
        for (blob,) in rows:
            ev = json.loads(blob)
            out.append({
                "ticker": ev["ticker"],
                "move_pct": ev.get("move_pct"),
                "pattern": ev["pattern"]["id"],
                "tier": ev["confidence"]["tier"],
                "confidence": ev["confidence"]["value"],
                "residual": ev["unexplained_residual"],
                "residual_basis": ev.get("residual_basis"),
                "data_source": ev.get("data_source"),
                "proxy_data": ev.get("proxy_data", False),
                "move_class": ev.get("move_class"),
                "initiating": ev["timeline"][0]["label"] if ev.get("timeline") else None,
            })
        out.sort(key=lambda r: r["confidence"], reverse=True)
        return JSONResponse(out)

    @app.get("/api/card/{ticker}", response_class=PlainTextResponse)
    def card(ticker: str, date: str):
        from ..outputs.render import render_card

        d = _parse(date)
        con = _con()
        try:
            row = con.execute(
                "SELECT evidence_object FROM move_explanations "
                "WHERE ticker=? AND CAST(window_start AS DATE)=?", [ticker.upper(), d]).fetchone()
        finally:
            con.close()
        if not row:
            raise HTTPException(404, f"No card for {ticker} on {d}")
        return render_card(json.loads(row[0]))

    @app.get("/api/postmortem/{date}", response_class=PlainTextResponse)
    def postmortem(date: str):
        from ..outputs.postmortem import build_context
        from ..outputs.render import render_postmortem

        d = _parse(date)
        con = _con()
        try:
            rows = con.execute(
                "SELECT evidence_object FROM move_explanations WHERE CAST(window_start AS DATE)=?",
                [d]).fetchall()
        finally:
            con.close()
        evidences = [json.loads(b) for (b,) in rows]
        if not evidences:
            raise HTTPException(404, f"No explanations for {d}; run `meridian postmortem --date {d}`.")
        return render_postmortem(build_context(cfg, d, evidences))

    @app.get("/api/health")
    def health():
        return {"ok": cfg.duckdb_path.exists(), "db": str(cfg.duckdb_path)}

    return app


def _parse(date: str) -> dt.date:
    try:
        return dt.date.fromisoformat(date)
    except ValueError:
        from fastapi import HTTPException
        raise HTTPException(400, f"bad date {date!r}")
