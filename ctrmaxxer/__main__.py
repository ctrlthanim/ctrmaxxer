"""CLI:  python -m ctrmaxxer {check|probe|run|loop|status}"""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone

from .agent import load_config, log, make_roblox, print_status, run_cycle
from .roblox import RobloxError


def check(cfg):
    rb = make_roblox(cfg)
    print("Advertisable experiences:", json.dumps(rb.advertisable_universes(), indent=2))
    print("Campaign options:", json.dumps(rb.campaign_options(cfg["game"]["universe_id"]), indent=2))


def probe(cfg, names, dimension):
    """Try candidate ad metric names against the Analytics Query API and show what comes back."""
    rb = make_roblox(cfg)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=30)
    for name in names:
        try:
            vals = rb.query_metric(cfg["game"]["universe_id"], name, start, end,
                                   breakdown=[dimension] if dimension else None)
            print(f"OK   {name}: {json.dumps(vals)[:400]}")
        except RobloxError as e:
            print(f"FAIL {name}: {e}")


def main():
    p = argparse.ArgumentParser(prog="ctrmaxxer")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify Roblox API key + ad eligibility")
    pr = sub.add_parser("probe", help="find the right ad metric names")
    pr.add_argument("names", nargs="*", default=["AdImpressions", "AdClicks", "AdSpend",
                                                 "AdsImpressions", "AdsClicks", "Impressions", "Clicks"])
    pr.add_argument("--dimension", default=None, help="breakdown to try, e.g. AdCampaignId")
    r = sub.add_parser("run", help="run one cycle")
    r.add_argument("--live", action="store_true", help="actually upload + launch (default: dry run)")
    lp = sub.add_parser("loop", help="run cycles forever")
    lp.add_argument("--live", action="store_true")
    lp.add_argument("--hours", type=float, default=None, help="hours between cycles (default from config)")
    sub.add_parser("status", help="leaderboard")
    a = p.parse_args()

    if a.cmd == "status":
        return print_status()
    cfg = load_config()
    if a.cmd == "check":
        check(cfg)
    elif a.cmd == "probe":
        probe(cfg, a.names, a.dimension)
    elif a.cmd == "run":
        run_cycle(cfg, dry_run=not a.live)
    elif a.cmd == "loop":
        hours = a.hours or cfg["schedule"]["hours_between_cycles"]
        while True:
            try:
                run_cycle(load_config(), dry_run=not a.live)
            except Exception as e:  # keep the loop alive; the next cycle retries
                log(f"cycle crashed: {e!r}")
            log(f"sleeping {hours}h")
            time.sleep(hours * 3600)


if __name__ == "__main__":
    main()
