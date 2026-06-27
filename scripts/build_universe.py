"""Build config/universe.csv = S&P 500 ∪ Nasdaq-100.

Source of record:
  - S&P 500: datasets/s-and-p-500-companies on GitHub (GICS sector included).
  - Nasdaq-100: the index is ~90% contained in the S&P 500. NDX_ONLY below holds
    Nasdaq-100 members that are typically NOT in the S&P 500 (mostly foreign-
    domiciled, hence S&P-ineligible). VERIFY/REFRESH periodically.

Usage:  python scripts/build_universe.py
Output: config/universe.csv  columns: symbol,name,sector,index_membership
"""
import csv
import io
import pathlib
import urllib.request

SP_URL = (
    "https://raw.githubusercontent.com/datasets/"
    "s-and-p-500-companies/main/data/constituents.csv"
)

# Nasdaq-100 members commonly NOT in the S&P 500. Verify on each refresh.
NDX_ONLY = [
    ("ARM",  "Arm Holdings plc",      "Information Technology"),
    ("PDD",  "PDD Holdings Inc",      "Consumer Discretionary"),
    ("MELI", "MercadoLibre Inc",      "Consumer Discretionary"),
    ("TEAM", "Atlassian Corporation", "Information Technology"),
    ("ASML", "ASML Holding NV",       "Information Technology"),
    ("AZN",  "AstraZeneca plc",       "Health Care"),
]


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=30).read().decode()


def main() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    out = root / "config" / "universe.csv"
    rows: dict[str, tuple[str, str, str]] = {}

    for r in csv.DictReader(io.StringIO(fetch(SP_URL))):
        sym = r["Symbol"].strip().replace(".", "-")
        rows[sym] = (r["Security"].strip(), r["GICS Sector"].strip(), "SP500")

    for sym, name, sector in NDX_ONLY:
        if sym in rows:
            rows[sym] = (rows[sym][0], rows[sym][1], "SP500+NDX100")
        else:
            rows[sym] = (name, sector, "NDX100")

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "name", "sector", "index_membership"])
        for sym in sorted(rows):
            name, sector, mem = rows[sym]
            w.writerow([sym, name, sector, mem])
    print(f"Wrote {len(rows)} symbols -> {out}")


if __name__ == "__main__":
    main()
