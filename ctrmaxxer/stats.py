"""Bayesian CTR judging, so the agent doesn't crown winners off noise."""
import random


def ctr(row):
    return row["clicks"] / row["impressions"] if row["impressions"] else 0.0


def prob_best(rows, min_impressions=1, samples=4000, seed=0):
    """P(each creative has the highest true CTR), via Beta(1+clicks, 1+misses) sampling.

    Only creatives with >= min_impressions compete; an untested one would otherwise look
    like a coin flip and drag every proven creative's P(best) toward zero."""
    rows = [r for r in rows if r["impressions"] >= max(min_impressions, 1)]
    if not rows:
        return {}
    rng = random.Random(seed)
    wins = {r["id"]: 0 for r in rows}
    params = [(r["id"], 1 + r["clicks"], 1 + max(r["impressions"] - r["clicks"], 0)) for r in rows]
    for _ in range(samples):
        best_id, best = None, -1.0
        for cid, a, b in params:
            v = rng.betavariate(a, b)
            if v > best:
                best_id, best = cid, v
        wins[best_id] += 1
    return {cid: w / samples for cid, w in wins.items()}


def pick_losers(live, min_impressions, prune_below):
    """Creatives with enough data whose chance of being the best has collapsed."""
    pb = prob_best(live, min_impressions)
    return [r for r in live if r["id"] in pb and pb[r["id"]] < prune_below], pb
