"""One optimization cycle: measure -> prune losers -> study winners -> generate -> launch."""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import discord, metrics
from .gemini import Gemini
from .roblox import Roblox, RobloxError
from .stats import ctr, pick_losers, prob_best
from .store import Store, now

ROOT = Path(__file__).resolve().parent.parent


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config(path=ROOT / "config.yaml"):
    load_dotenv(ROOT / ".env")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    env = lambda k: os.environ.get(k, "").strip()
    cfg["secrets"] = {"roblox": env("ROBLOX_API_KEY"), "gemini": env("GEMINI_API_KEY")}
    creator = {"groupId": env("ROBLOX_CREATOR_GROUP_ID")} if env("ROBLOX_CREATOR_GROUP_ID") \
        else {"userId": env("ROBLOX_CREATOR_USER_ID")}
    cfg["creator"] = creator
    return cfg


def make_roblox(cfg):
    if not cfg["secrets"]["roblox"]:
        raise SystemExit("ROBLOX_API_KEY missing from .env")
    return Roblox(cfg["secrets"]["roblox"], cfg["creator"])


def make_gemini(cfg):
    if not cfg["secrets"]["gemini"]:
        raise SystemExit("GEMINI_API_KEY missing from .env")
    g = cfg["gemini"]
    return Gemini(cfg["secrets"]["gemini"], g["image_model"], g["text_model"], g["aspect_ratio"],
                  g.get("output_size"))


def days_live(row):
    if not row["launched_at"]:
        return 0.0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(row["launched_at"])).total_seconds() / 86400


def seed_images():
    d = ROOT / "seeds"
    return sorted(p for p in d.glob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg")) if d.exists() else []


def refresh(store, rb, cfg):
    """Pull delivery status + metrics for every live creative."""
    live = store.by_status("live")
    if not live:
        return
    statuses = rb.campaign_statuses([r["campaign_id"] for r in live])
    for r in live:
        s = statuses.get(r["campaign_id"])
        if not s:
            continue
        if s.get("deliveryStatus") == "REJECTED":
            store.update(r["id"], status="rejected", note=f"Rejected by Roblox review: {s.get('deliveryStatusReasons')}")
            log(f"creative #{r['id']} REJECTED by review")
            discord.post(f"Ad #{r['id']} rejected by Roblox review", ", ".join(s.get("deliveryStatusReasons") or []),
                         "bad", image=r["path"])
        elif s.get("status") in ("PAUSED", "CANCELLED"):
            store.update(r["id"], status="paused", note=f"{s['status']} outside the agent")
    stats = metrics.fetch(rb, cfg, cfg["game"]["universe_id"])
    budget = cfg["guardrails"]["daily_budget_per_campaign_usd"]
    for r in store.by_status("live", "paused"):
        m = stats.get(r["campaign_id"])
        if m:
            # Without a spend metric, assume the full daily budget was spent (conservative).
            spend = m["spend_usd"] or (budget * days_live(r) if r["status"] == "live" else r["spend_usd"])
            store.update(r["id"], impressions=m["impressions"], clicks=m["clicks"], spend_usd=spend)
        elif r["status"] == "live":
            store.update(r["id"], spend_usd=budget * days_live(r))


def run_cycle(cfg, dry_run):
    g = cfg["guardrails"]
    if (ROOT / "STOP").exists():
        log("STOP file present -> doing nothing. Delete it to resume.")
        return
    store = Store(ROOT / "ctrmaxxer.db")
    rb = None if dry_run else make_roblox(cfg)
    mode = "DRY RUN (no Roblox writes)" if dry_run else "LIVE"
    log(f"cycle start - {mode}")

    if rb:
        refresh(store, rb, cfg)

    # Hard spend cap: pause everything and stop.
    spent = store.total_spend()
    if spent >= g["max_total_spend_usd"]:
        log(f"spend ${spent:.2f} >= cap ${g['max_total_spend_usd']} -> pausing all campaigns")
        for r in store.by_status("live"):
            if rb:
                rb.set_campaign_status(r["campaign_id"], "PAUSED")
            store.update(r["id"], status="paused", note="paused: total spend cap reached")
        if rb:
            discord.post("Spend cap reached: all ads paused",
                         f"${spent:.2f} spent of the ${g['max_total_spend_usd']} lifetime cap.", "warn")
        return

    # Prune creatives that have clearly lost.
    live = store.by_status("live")
    losers, pb = pick_losers(live, g["min_impressions_to_judge"], g["prune_if_prob_best_below"])
    keep_min = 1  # never pause the last one standing
    for r in losers:
        if len(live) - 1 < keep_min:
            break
        log(f"pausing loser #{r['id']}: ctr {ctr(r) * 100:.3f}% on {r['impressions']} imps, P(best)={pb[r['id']]:.3f}")
        if rb:
            rb.set_campaign_status(r["campaign_id"], "PAUSED")
            discord.post(f"Paused losing ad #{r['id']}", r["hypothesis"] or "", "warn", fields=[
                ("CTR", f"{ctr(r) * 100:.3f}%"), ("Impressions", r["impressions"]),
                ("Chance it's the best", f"{pb[r['id']] * 100:.1f}%")])
        store.update(r["id"], status="paused", note=f"pruned: P(best)={pb[r['id']]:.3f}")
        live = [x for x in live if x["id"] != r["id"]]

    # How many new creatives can we afford to launch?
    budget = g["daily_budget_per_campaign_usd"]
    max_by_budget = int(g["max_total_daily_budget_usd"] // budget)
    slots = min(g["max_active_campaigns"], max_by_budget) - len(live)
    n_new = max(0, min(slots, g["max_new_per_cycle"]))
    if n_new == 0:
        log(f"{len(live)} live, no free slots - waiting for data")
        if rb:
            post_summary(store, "No free slots: waiting for more data.")
        return

    # Study everything tested so far and plan the next batch.
    gm = make_gemini(cfg)
    judged = [r for r in store.all() if r["status"] in ("live", "paused", "rejected")]
    plan = gm.plan_next(cfg["game"], judged, prob_best(judged, g["min_impressions_to_judge"]),
                        store.recent_insights(), n_new, seed_images())
    store.add_insight(plan.get("insights", ""))
    log(f"insights: {plan.get('insights', '')}")

    out_dir = ROOT / "creatives"
    for prop in plan["proposals"]:
        base = store.get(prop["base_id"]) if prop.get("mode") == "edit" and prop.get("base_id") else None
        refs = [base["path"]] if base else seed_images()[:3]
        gen = (base["generation"] + 1) if base else 0
        cid = store.add_creative("", prop["prompt"], prop.get("hypothesis"), base["id"] if base else None, gen)
        path = out_dir / f"c{cid:04d}_g{gen}.png"
        try:
            gm.make_image(prop["prompt"], path, refs)
        except Exception as e:
            store.update(cid, path=str(path), status="failed", note=f"generation failed: {e}")
            log(f"#{cid} generation failed: {e}")
            if rb:
                discord.post(f"Ad #{cid}: image generation failed", str(e)[:500], "bad")
            continue
        store.update(cid, path=str(path))
        log(f"#{cid} generated ({prop.get('mode')}, gen {gen}): {prop.get('hypothesis')}")
        if not rb:
            store.update(cid, note="dry run: not launched")
            continue
        try:
            asset_id = rb.upload_image(path, f"CTRMaxxer c{cid}", prop.get("hypothesis") or "")
            camp = rb.create_campaign(
                name=f"{cfg['campaign']['name_prefix']} c{cid} g{gen}",
                universe_id=cfg["game"]["universe_id"], asset_ids=[asset_id], daily_budget_usd=budget,
                duration_days=cfg["campaign"]["duration_days"], payment_type=cfg["campaign"]["payment_type"],
                targeting=cfg["campaign"].get("targeting"))
            store.update(cid, asset_id=asset_id, campaign_id=str(camp["id"]), status="live", launched_at=now())
            log(f"#{cid} launched: asset {asset_id}, campaign {camp['id']} (${budget}/day)")
            discord.post(f"Launched ad #{cid}", prop.get("hypothesis") or "", "good", image=path, fields=[
                ("Budget", f"${budget}/day for {cfg['campaign']['duration_days']} days"),
                ("Type", f"{prop.get('mode')} (gen {gen})"), ("Campaign", str(camp["id"]))])
        except RobloxError as e:
            store.update(cid, status="failed", note=str(e)[:500])
            log(f"#{cid} launch failed: {e}")
            discord.post(f"Ad #{cid}: launch failed", str(e)[:500], "bad")
    log("cycle done")
    if rb:
        post_summary(store, plan.get("insights", ""))


def post_summary(store, note=""):
    """The leaderboard of everything that has run, as one Discord message."""
    rows = store.by_status("live", "paused", "rejected")
    if not rows:
        return
    pb = prob_best([r for r in rows if r["status"] != "rejected"])
    lines = []
    for r in sorted(rows, key=lambda r: -pb.get(r["id"], -1)):
        lines.append(f"`#{r['id']:<3}` {r['status']:<8} {r['impressions']:>7,} imps  {r['clicks']:>5,} clicks  "
                     f"**{ctr(r) * 100:.2f}%**  best? {pb.get(r['id'], 0) * 100:.0f}%")
    text = "\n".join(lines)
    if note:
        text += f"\n\n_{note[:600]}_"
    discord.post("Cycle summary", text, "info", fields=[("Total spend", f"${store.total_spend():.2f}")])


WATCH_STATE = ROOT / "watch_state.json"
# deliveryStatus -> (headline, colour, image attached?)
DELIVERY = {
    "IN_REVIEW": ("in review", "info", False),
    "SERVING": ("now showing to players", "good", True),
    "NOT_SERVING": ("not showing", "warn", False),
    "REJECTED": ("rejected by Roblox review", "bad", True),
}
REASONS = {"SCHEDULED": "waiting for its start time"}


def watch_once(rb, store, state):
    """Post every live/paused ad whose Roblox status changed since the last check."""
    rows = [r for r in store.by_status("live", "paused") if r["campaign_id"]]
    if not rows:
        return
    statuses = rb.campaign_statuses([r["campaign_id"] for r in rows])
    for r in rows:
        s = statuses.get(r["campaign_id"])
        if not s:
            continue
        reasons = s.get("deliveryStatusReasons") or []
        key = f"{s.get('status')}/{s.get('deliveryStatus')}/{','.join(reasons)}"
        if state.get(r["campaign_id"]) == key:
            continue
        state[r["campaign_id"]] = key
        delivery = s.get("deliveryStatus", "")
        headline, color, with_image = DELIVERY.get(delivery, (delivery.lower().replace("_", " "), "info", False))
        if s.get("status") == "PAUSED":
            headline, color = "paused", "warn"
        why = ", ".join(REASONS.get(x, x.lower().replace("_", " ")) for x in reasons)
        log(f"#{r['id']} {headline}{f' ({why})' if why else ''}")
        discord.post(f"Ad #{r['id']}: {headline}", why, color, image=r["path"] if with_image else None)
        if delivery == "REJECTED" and r["status"] != "rejected":
            store.update(r["id"], status="rejected", note=f"Rejected by Roblox review: {reasons}")
    WATCH_STATE.write_text(json.dumps(state, indent=1))


def watch(cfg, minutes):
    rb = make_roblox(cfg)
    store = Store(ROOT / "ctrmaxxer.db")
    state = json.loads(WATCH_STATE.read_text()) if WATCH_STATE.exists() else {}
    log(f"watching ad status every {minutes} min (Discord: {'on' if discord.enabled() else 'off, no DISCORD_WEBHOOK_URL'})")
    while True:
        try:
            watch_once(rb, store, state)
        except Exception as e:  # keep watching; the next check retries
            log(f"watch check failed: {e!r}")
        time.sleep(minutes * 60)


def print_status():
    store = Store(ROOT / "ctrmaxxer.db")
    rows = store.all()
    tested = [r for r in rows if r["status"] in ("live", "paused")]
    pb = prob_best(tested)
    print(f"{'id':>4} {'gen':>3} {'status':<9} {'imps':>8} {'clicks':>7} {'ctr%':>7} {'P(best)':>7}  hypothesis")
    for r in sorted(rows, key=lambda r: (-pb.get(r["id"], -1), r["id"])):
        print(f"{r['id']:>4} {r['generation']:>3} {r['status']:<9} {r['impressions']:>8} {r['clicks']:>7} "
              f"{ctr(r) * 100:>7.3f} {pb.get(r['id'], 0):>7.3f}  {(r['hypothesis'] or '')[:60]}")
    print(f"\ntotal spend (reported or estimated): ${store.total_spend():.2f}")
    for i in store.recent_insights(1):
        print(f"latest insight: {i}")
