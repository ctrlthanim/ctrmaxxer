# CTRMaxxer

An agent that runs your Roblox ad creatives like an evolution loop. Every cycle it:

1. **Measures.** It pulls impressions and clicks for every live ad. Each creative runs as its own campaign, so this gives CTR per image.
2. **Prunes.** It pauses any creative that has had enough impressions and has less than a 5% chance of being the best (Bayesian, so it doesn't crown winners off noise).
3. **Studies.** A Gemini "brain" looks at the leaderboard and the actual winning and losing images, then writes down *why* winners win.
4. **Breeds.** It proposes new creatives: mostly Nano Banana *edits* of the current best image, each testing one hypothesis, plus some bold new concepts.
5. **Launches.** It uploads each image through the Open Cloud Assets API and creates a campaign through the Roblox Ads API, staying inside your budget caps.

Every image, prompt, hypothesis, parent and result is stored in `ctrmaxxer.db`, so the brain learns across cycles.

## Setup

```bash
python -m pip install -r requirements.txt
copy .env.example .env      # then fill in the keys
```

- **Roblox API key** (Creator Hub → Open Cloud → API Keys): `ad.campaign:read/write`, `ad.billing:read`, `asset:read/write`, `universe.analytics:read`.
- **Gemini API key**: from Google AI Studio.
- **config.yaml**: set your `universe_id`, game description and guardrails.
- **seeds/** (optional): drop in screenshots or current thumbnails of your game. New concepts get generated from them, so ads look like the real game.

## Run

```bash
python -m ctrmaxxer check           # key works + experience is eligible for ads?
python -m ctrmaxxer run             # DRY RUN: generates images into creatives/, launches nothing
python -m ctrmaxxer run --live      # one real cycle
python -m ctrmaxxer loop --live     # forever, every 12h (config: schedule.hours_between_cycles)
python -m ctrmaxxer status          # leaderboard
```

**Kill switch:** create a file named `STOP` in this folder. The next cycle does nothing.

## Discord updates (optional)

In Discord: channel settings → **Integrations → Webhooks → New Webhook → Copy Webhook URL**, and put it in `.env` as `DISCORD_WEBHOOK_URL`. Then:

```bash
python -m ctrmaxxer discord-test    # sends a test message
python -m ctrmaxxer watch           # posts when an ad goes into review, starts showing, gets rejected or paused
```

Live cycles post on their own: each launch (with the ad image), paused losers, rejections, the spend cap, failures, and a leaderboard summary at the end. `watch` checks every `schedule.watch_minutes` (15) and only posts changes; keep it running in its own terminal. Without a webhook URL nothing is posted.

## Getting CTR data (read this)

The Ads API docs say campaign metrics come from the Open Cloud Analytics API, but Roblox doesn't publish the ad metric names. Two options:

- **`metrics.source: csv`** (default, always works): export the campaign report from Ads Manager and save it as `metrics.csv` with columns `campaign_id, impressions, clicks, spend` before each cycle.
- **`metrics.source: analytics`** (fully hands-off): run `python -m ctrmaxxer probe` (add names or `--dimension X` to try others) to find the real metric and breakdown names, put them in `config.yaml`, then switch the source.

## Guardrails

- Every creative gets its own campaign at `daily_budget_per_campaign_usd`.
- Total daily budget can never exceed `max_total_daily_budget_usd`.
- Lifetime cap `max_total_spend_usd`: when it's reached, everything gets paused. Without a spend metric, the agent assumes each live campaign spends its full daily budget.
- No creative is judged before `min_impressions_to_judge`, and the last live creative is never paused.
- The agent only ever **pauses**. It never cancels, because cancelling is permanent on Roblox.
- Rejected creatives get recorded, so the brain can avoid what review didn't like.
