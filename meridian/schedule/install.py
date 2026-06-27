"""`meridian install-daily` (Phase 8 packaging): set up scheduled local runs.

macOS  -> a launchd LaunchAgent that keeps `meridian schedule --mode both` running
          (RunAtLoad + KeepAlive), so pre-market / intraday / post-close all fire.
other  -> prints the equivalent crontab lines.
Writing the file is local + reversible; loading it (an outward action) only happens
with --activate.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sys
from dataclasses import dataclass

from ..config import Config

LABEL = "com.meridian.daily"


@dataclass
class InstallPlan:
    platform: str
    path: pathlib.Path | None
    content: str
    activate_cmd: str
    notes: str


def _meridian_cmd() -> list[str]:
    exe = shutil.which("meridian")
    if exe:
        return [exe]
    return [sys.executable, "-m", "meridian.cli"]


def build_plan(cfg: Config) -> InstallPlan:
    cmd = _meridian_cmd()
    workdir = str(cfg.root)
    logdir = cfg.root / "data"
    if sys.platform == "darwin":
        args = cmd + ["schedule", "--mode", "both"]
        prog = "\n".join(f"    <string>{a}</string>" for a in args)
        path = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{prog}
  </array>
  <key>WorkingDirectory</key><string>{workdir}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{logdir / 'meridian.out.log'}</string>
  <key>StandardErrorPath</key><string>{logdir / 'meridian.err.log'}</string>
</dict>
</plist>
"""
        return InstallPlan("darwin", path, content,
                           activate_cmd=f"launchctl load {path}",
                           notes="Keeps the in-proc scheduler alive; it crons pre-market, "
                                 "intraday, and post-close jobs (America/New_York).")
    # cron fallback
    base = " ".join(cmd)
    cron = (f"30 8 * * 1-5 cd {workdir} && {base} run-day --date $(date +\\%F) >> "
            f"{logdir / 'meridian.out.log'} 2>&1\n"
            f"35 16 * * 1-5 cd {workdir} && {base} postmortem --date $(date +\\%F) >> "
            f"{logdir / 'meridian.out.log'} 2>&1\n")
    return InstallPlan(sys.platform, None, cron,
                       activate_cmd="crontab -e   # then paste the lines above",
                       notes="Cron runs the EOD batch + postmortem on weekdays (local time).")


def install_daily(cfg: Config, activate: bool = False) -> InstallPlan:
    plan = build_plan(cfg)
    (cfg.root / "data").mkdir(parents=True, exist_ok=True)
    if plan.path is not None:
        plan.path.parent.mkdir(parents=True, exist_ok=True)
        plan.path.write_text(plan.content)
        if activate:
            os.system(f"launchctl unload {plan.path} 2>/dev/null; launchctl load {plan.path}")
    return plan
