"""Discord updates through channel webhooks. Set DISCORD_WEBHOOK_URL in .env (several channels: separate the
URLs with commas); unset = nothing is posted. A failed post is printed and ignored, so Discord being down
never breaks a cycle."""
import json
import os
from pathlib import Path

import requests

COLORS = {"good": 0x3CAA50, "bad": 0xD24637, "warn": 0xE0A030, "info": 0x4678C8}


def urls():
    return [u.strip() for u in os.environ.get("DISCORD_WEBHOOK_URL", "").split(",") if u.strip()]


def enabled():
    return bool(urls())


def post(title, text="", color="info", fields=(), image=None):
    """One embed to every webhook: title, text, a coloured bar, inline fields [(name, value)], an image file."""
    targets = urls()
    if not targets:
        return
    embed = {"title": title[:256], "description": text[:4000], "color": COLORS.get(color, COLORS["info"])}
    if fields:
        embed["fields"] = [{"name": str(k)[:256], "value": str(v)[:1024] or "-", "inline": True} for k, v in fields]
    image = Path(image) if image else None
    if image and image.exists():
        embed["image"] = {"url": f"attachment://{image.name}"}
    else:
        image = None
    payload = {"username": "CTRMaxxer", "embeds": [embed]}
    for i, url in enumerate(targets, 1):
        try:
            if image:
                with open(image, "rb") as f:
                    r = requests.post(url, data={"payload_json": json.dumps(payload)},
                                      files={"files[0]": (image.name, f)}, timeout=60)
            else:
                r = requests.post(url, json=payload, timeout=15)
            if not r.ok:
                print(f"[discord] webhook {i}: post failed {r.status_code}: {r.text[:200]}", flush=True)
        except requests.RequestException as e:
            print(f"[discord] webhook {i}: post failed: {e}", flush=True)
