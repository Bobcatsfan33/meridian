"""Configuration loader. Reads config/config.yaml into a typed object."""
from __future__ import annotations
import pathlib
from dataclasses import dataclass, field
from typing import Any
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = REPO_ROOT / "config" / "config.yaml"


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)
    root: pathlib.Path = REPO_ROOT

    @classmethod
    def load(cls, path: str | pathlib.Path | None = None) -> "Config":
        p = pathlib.Path(path) if path else DEFAULT_CONFIG
        with open(p) as f:
            raw = yaml.safe_load(f) or {}
        return cls(raw=raw, root=REPO_ROOT)

    @property
    def duckdb_path(self) -> pathlib.Path:
        rel = self.raw.get("storage", {}).get("duckdb_path", "data/meridian.duckdb")
        return self.root / rel

    @property
    def universe_file(self) -> pathlib.Path:
        rel = self.raw.get("universe", {}).get("file", "config/universe.csv")
        return self.root / rel

    @property
    def index_etf_file(self) -> pathlib.Path:
        rel = self.raw.get("universe", {}).get("index_etfs", "config/index_etfs.csv")
        return self.root / rel

    @property
    def engine(self) -> dict[str, Any]:
        return self.raw.get("engine", {}) or {}

    @property
    def featurization(self) -> dict[str, Any]:
        """Layer-1 thresholds (the only place thresholds may live)."""
        return self.engine.get("featurization", {}) or {}

    def feat(self, key: str, default: Any = None) -> Any:
        return self.featurization.get(key, default)

    @property
    def causal_test_alpha(self) -> float:
        return float(self.engine.get("causal_test_alpha", 0.05))

    @property
    def match_cfg(self) -> dict[str, Any]:
        return self.engine.get("match", {}) or {}

    @property
    def patterns_dir(self) -> pathlib.Path:
        rel = self.raw.get("patterns", {}).get("dir", "config/patterns")
        return self.root / rel

    @property
    def predict(self) -> dict[str, Any]:
        return self.raw.get("predict", {}) or {}

    @property
    def watchlist(self) -> list[str]:
        """Pinned names (config.yaml: watchlist: [NVDA, AMD]) — always carded, shown on top."""
        wl = self.raw.get("watchlist") or []
        seen, out = set(), []
        for s in wl:
            t = str(s).strip().upper()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out
