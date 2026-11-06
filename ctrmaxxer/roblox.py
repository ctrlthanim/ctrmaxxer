"""Roblox Open Cloud clients: Assets (upload creatives), Ads Management, Analytics."""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://apis.roblox.com"


class RobloxError(RuntimeError):
    pass


class Roblox:
    def __init__(self, api_key: str, creator: dict, timeout: int = 30):
        self.creator = creator  # {"userId": "..."} or {"groupId": "..."}
        self.s = requests.Session()
        self.s.headers["x-api-key"] = api_key
        self.timeout = timeout

    def _req(self, method, path, **kw):
        for attempt in range(5):
            r = self.s.request(method, f"{BASE}{path}", timeout=self.timeout, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if not r.ok:
                raise RobloxError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
            return r.json() if r.content else {}
        raise RobloxError(f"{method} {path} -> gave up after retries ({r.status_code})")

    # ---------- Assets ----------
    def upload_image(self, path: Path, name: str, description: str = "") -> str:
        """Upload a PNG/JPEG as an image asset and return its assetId (waits for processing)."""
        req = {
            "assetType": "Decal",
            "displayName": name[:50],
            "description": description[:1000],
            "creationContext": {"creator": self.creator},
        }
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        with open(path, "rb") as f:
            op = self._req("POST", "/assets/v1/assets",
                           files={"request": (None, json.dumps(req)),
                                  "fileContent": (path.name, f, mime)})
        op_path = op["path"]  # "operations/{id}"
        for _ in range(60):
            res = self._req("GET", f"/assets/v1/{op_path}")
            if res.get("done"):
                resp = res.get("response", {})
                if "assetId" not in resp:
                    raise RobloxError(f"Upload finished without assetId: {res}")
                mod = resp.get("moderationResult", {}).get("moderationState")
                if mod and mod not in ("Approved", "MODERATION_STATE_APPROVED"):
                    raise RobloxError(f"Asset {resp['assetId']} moderation: {mod}")
                return str(resp["assetId"])
            time.sleep(2)
        raise RobloxError(f"Timed out waiting for {op_path}")

    # ---------- Ads Management ----------
    def advertisable_universes(self):
        return self._req("GET", "/ads-management/v1/advertisable-universes")

    def campaign_options(self, universe_id: str):
        return self._req("GET", "/ads-management/v1/campaign-options",
                         params={"universeId": universe_id})

    def create_campaign(self, *, name, universe_id, asset_ids, daily_budget_usd,
                        duration_days, payment_type, targeting) -> dict:
        start = datetime.now(timezone.utc) + timedelta(minutes=10)
        body = {
            "name": name[:100],
            "objective": "ENGAGEMENT",
            "paymentType": payment_type,
            "targetUniverseId": str(universe_id),
            "creativeAssetIds": [str(a) for a in asset_ids],
            "budget": {"type": "DAILY", "amountMicros": str(int(round(daily_budget_usd * 1_000_000)))},
            "schedule": {"startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "durationInDays": int(duration_days)},
            "targeting": targeting or {},
        }
        return self._req("POST", "/ads-management/v1/campaigns", json=body,
                         headers={"x-idempotency-key": str(uuid.uuid4())})

    def set_campaign_status(self, campaign_id: str, status: str):
        assert status in ("ACTIVE", "PAUSED")  # never auto-CANCEL: it's permanent
        return self._req("PATCH", f"/ads-management/v1/campaigns/{campaign_id}",
                         json={"status": status})

    def campaign_statuses(self, ids):
        out = {}
        ids = list(ids)
        for i in range(0, len(ids), 100):
            res = self._req("POST", "/ads-management/v1/campaigns:batchGetStatus",
                            json={"campaignIds": ids[i:i + 100]})
            for s in res.get("statuses", []):
                out[str(s["id"])] = s
        return out

    # ---------- Analytics ----------
    def query_metric(self, universe_id, metric, start, end, breakdown=None,
                     filters=None, granularity="None"):
        body = {
            "metric": metric,
            "granularity": granularity,
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if breakdown:
            body["breakdown"] = breakdown
        if filters:
            body["filter"] = filters
        res = self._req("POST", f"/analytics-query-api/v1/universes/{universe_id}/metrics", json=body)
        for _ in range(60):
            if res.get("done", True):
                return res.get("response", {}).get("values", [])
            time.sleep(2)
            res = self._req("GET", f"/analytics-query-api/{res['path']}")
        raise RobloxError(f"Analytics query for {metric} never finished")
