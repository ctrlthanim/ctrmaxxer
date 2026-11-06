"""Nano Banana image generation + the 'brain' that studies results and writes the next prompts."""
import json
import time
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from PIL import Image

from .stats import ctr

BRAIN_SYSTEM = """You are a Roblox ad creative strategist whose only goal is maximizing click-through rate.
You see the leaderboard of ad images tested so far (with real impressions/clicks) plus the images themselves.
Work out WHY winners win (composition, characters, faces/emotion, color contrast, text, focal point,
readability at small mobile sizes, curiosity/stakes) and why losers lose. Then propose the next creatives.

Rules:
- Most proposals should EDIT the current best image (mode "edit") to test one clear hypothesis each.
- Some should be bold NEW concepts (mode "new") so the search doesn't get stuck on one idea.
- Images must look like the actual game, be on-brand for Roblox, and follow Roblox ad policy:
  no misleading UI (fake buttons/play icons), no real-money/gambling claims, no off-platform links,
  nothing violent, scary or sexual beyond what suits 13+, no copyrighted characters you don't own.
- Treat creatives with few impressions as unproven; don't over-learn from them.

Return JSON only:
{"insights": "<3-6 sentences: what drives CTR for this game so far>",
 "proposals": [{"mode": "edit"|"new", "base_id": <creative id to edit, or null>,
                "hypothesis": "<what this tests>", "prompt": "<full image-generation/editing prompt>"}]}"""


def _with_retry(fn, tries=6):
    """Retry Gemini's transient overload/rate-limit errors (429/500/503) with backoff."""
    for attempt in range(tries):
        try:
            return fn()
        except (genai_errors.ServerError, genai_errors.ClientError) as e:
            if getattr(e, "code", None) not in (429, 500, 503) or attempt == tries - 1:
                raise
            if "limit: 0" in str(e) or "per day" in str(e):
                # Quota is zero (free tier) or the daily cap is spent: waiting minutes won't help.
                raise RuntimeError("Gemini quota exhausted. Image models need billing enabled on "
                                   "the API key's Google Cloud project (aistudio.google.com).") from e
            wait = min(10 * 2 ** attempt, 120)
            print(f"  Gemini busy ({e.code}), retrying in {wait}s...", flush=True)
            time.sleep(wait)


class Gemini:
    def __init__(self, api_key, image_model, text_model, aspect_ratio, output_size=None):
        self.client = genai.Client(api_key=api_key)
        self.image_model = image_model
        self.text_model = text_model
        self.aspect_ratio = aspect_ratio
        self.output_size = tuple(output_size) if output_size else None

    def make_image(self, prompt, out_path: Path, base_images=()):
        """Text -> image, or (with base_images) edit those images per the prompt."""
        contents = [prompt, *[Image.open(p) for p in base_images]]
        resp = _with_retry(lambda: self.client.models.generate_content(
            model=self.image_model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio=self.aspect_ratio),
            ),
        ))
        for part in resp.parts or []:
            if part.inline_data is not None:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                part.as_image().save(out_path)
                if self.output_size:  # Roblox ads only accept exact sizes (1920x1080 or 1024x576)
                    Image.open(out_path).convert("RGB").resize(self.output_size, Image.LANCZOS).save(out_path)
                return out_path
        raise RuntimeError(f"Nano Banana returned no image (likely blocked). Prompt: {prompt[:120]}...")

    def plan_next(self, game, rows, prob_best, insights, n, seed_images=()):
        """Look at the leaderboard + top/bottom images and propose n new creatives."""
        ranked = sorted(rows, key=lambda r: prob_best.get(r["id"], 0), reverse=True)
        board = [{
            "id": r["id"], "status": r["status"], "generation": r["generation"],
            "impressions": r["impressions"], "clicks": r["clicks"], "ctr_pct": round(ctr(r) * 100, 3),
            "prob_best": round(prob_best.get(r["id"], 0), 3), "parent_id": r["parent_id"],
            "hypothesis": r["hypothesis"], "note": r["note"],
        } for r in ranked]

        parts = [
            f"GAME: {game['name']}\n{game['description']}\nAudience/notes: {game.get('audience', '')}",
            f"PAST INSIGHTS (oldest first): {json.dumps(insights)}",
            f"LEADERBOARD (best first): {json.dumps(board)}",
            f"Propose exactly {n} creatives.",
        ]
        shown = ranked[:3] + [r for r in ranked[-2:] if r not in ranked[:3]]
        for r in shown:
            if Path(r["path"]).exists():
                parts += [f"Image for creative id {r['id']} (ctr {ctr(r) * 100:.3f}%):", Image.open(r["path"])]
        for p in seed_images:
            parts += [f"Reference screenshot/art from the actual game ({Path(p).name}):", Image.open(p)]

        resp = _with_retry(lambda: self.client.models.generate_content(
            model=self.text_model,
            contents=parts,
            config=types.GenerateContentConfig(system_instruction=BRAIN_SYSTEM,
                                               response_mime_type="application/json"),
        ))
        plan = json.loads(resp.text)
        # The model sometimes wraps the object, or nests proposals, in extra lists: keep any proposal dicts.
        if isinstance(plan, list):
            plan = next((p for p in plan if isinstance(p, dict)), {})
        proposals = []

        def collect(x):
            if isinstance(x, dict) and x.get("prompt"):
                proposals.append(x)
            elif isinstance(x, list):
                for y in x:
                    collect(y)

        collect(plan.get("proposals", []))
        plan["proposals"] = proposals[:n]
        return plan
