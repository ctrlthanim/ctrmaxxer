"""Per-campaign impressions/clicks/spend. One campaign == one creative, so this is per-creative CTR.

Two sources:
  * "analytics": Roblox Analytics Query API. Roblox's docs say ad metrics live there but don't
    publish the metric names, so they're set in config.yaml (run `python -m ctrmaxxer probe` to find them).
  * "csv": a CSV exported from Ads Manager, dropped at metrics.csv_path, with columns
    campaign_id, impressions, clicks[, spend].
"""
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _sum_by_campaign(values, dimension):
    out = {}
    for v in values:
        key = next((b["value"] for b in v.get("breakdowns", []) if b["dimension"] == dimension), None)
        if key is None:
            continue
        out[str(key)] = out.get(str(key), 0) + sum(float(p.get("value") or 0) for p in v.get("dataPoints", []))
    return out


def from_analytics(rb, cfg, universe_id, lookback_days=60):
    m = cfg["metrics"]
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=lookback_days)
    dim = m["campaign_dimension"]

    def get(name):
        if not name:
            return {}
        return _sum_by_campaign(rb.query_metric(universe_id, name, start, end, breakdown=[dim]), dim)

    imps, clicks, spend = get(m["impressions_metric"]), get(m["clicks_metric"]), get(m.get("spend_metric"))
    return {cid: {"impressions": int(imps.get(cid, 0)), "clicks": int(clicks.get(cid, 0)),
                  "spend_usd": float(spend.get(cid, 0))} for cid in set(imps) | set(clicks)}


def from_csv(path):
    path = Path(path)
    if not path.exists():
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
            cid = row.get("campaign_id") or row.get("campaign id")
            if not cid:
                continue
            num = lambda k: float((row.get(k) or "0").replace(",", "").replace("$", "") or 0)
            out[cid] = {"impressions": int(num("impressions")), "clicks": int(num("clicks")),
                        "spend_usd": num("spend") or num("spent")}
    return out


def fetch(rb, cfg, universe_id):
    src = cfg["metrics"]["source"]
    if src == "csv":
        return from_csv(cfg["metrics"]["csv_path"])
    if src == "analytics":
        return from_analytics(rb, cfg, universe_id)
    raise ValueError(f"unknown metrics.source {src!r}")
