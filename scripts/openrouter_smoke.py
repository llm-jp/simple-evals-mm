"""Smoke-test the OpenRouter registry (sampler/openrouter_sampler.py MODELS): one tiny image
request per (model, effort) and print the serving provider, token counts, billed cost and the
head of the answer. Run this before a campaign to confirm the pinned endpoint answers, the
effort levels are accepted and reasoning tokens are separated.

    uv run python scripts/openrouter_smoke.py                         # every model, every listed effort (+ default)
    uv run python scripts/openrouter_smoke.py --models qwen/qwen3.8-27b,z-ai/glm-5.3-flash --efforts low,max
    uv run python scripts/openrouter_smoke.py --models moonshotai/kimi-k3 --efforts low,medium,high,max   # probe unknown levels
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from simple_evals_mm.common import SamplerAPIError  # noqa: E402
from simple_evals_mm.sampler.openrouter_sampler import MODELS, OpenRouterSampler  # noqa: E402

PROMPT = "この画像に描かれている図形とその色を、日本語で一文で答えてください。"


def _test_image():
    """A 512x512 PNG with a red circle on white, drawn on the fly."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (512, 512), "white")
    ImageDraw.Draw(img).ellipse((96, 96, 416, 416), fill=(220, 30, 30))
    return img


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default="", help="comma list of OpenRouter ids (default: all in MODELS)")
    ap.add_argument("--efforts", default="", help="comma list of effort levels to try (default: the registry's levels, plus 'default')")
    ap.add_argument("--image", default="", help="image path (default: a generated red circle)")
    ap.add_argument("--max-tokens", type=int, default=16384)
    a = ap.parse_args()
    models = a.models.split(",") if a.models else list(MODELS)
    image = a.image or _test_image()
    rows = []
    for mid in models:
        spec = MODELS.get(mid, {})
        efforts = a.efforts.split(",") if a.efforts else ["default", *(spec.get("efforts") or ())]
        for eff in efforts:
            t0 = time.time()
            try:
                s = OpenRouterSampler(f"openrouter/{mid}")
                s.max_new_tokens = a.max_tokens
                if eff != "default":
                    s.set_reasoning_effort(eff)
                ep = s.endpoint_info or {}
                r = s([s.pack_message(images=[image], instruction=PROMPT)])
                md = r.response_metadata or {}
                row = dict(model=mid, effort=eff, ok=True, provider=md.get("provider"), tag=ep.get("tag"), quant=ep.get("quantization"),
                           cap=ep.get("max_completion_tokens"), in_tok=r.input_tokens, out_tok=r.output_tokens, reason_tok=r.reasoning_tokens,
                           cost=md.get("cost_usd"), finish=r.finish_reason, secs=round(time.time() - t0, 1),
                           reasoning_head=(r.reasoning or "")[:60].replace("\n", " "), answer=(r.response_text or "")[:80].replace("\n", " "))
            except (SamplerAPIError, RuntimeError, ValueError) as e:
                row = dict(model=mid, effort=eff, ok=False, error=f"{type(e).__name__}: {str(e)[:300]}", secs=round(time.time() - t0, 1))
            rows.append(row)
            print(" | ".join(f"{k}={v}" for k, v in row.items()), flush=True)
    bad = [r for r in rows if not r["ok"]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} ok")
    for r in bad:
        print(f"  FAIL {r['model']} effort={r['effort']}: {r['error']}")


if __name__ == "__main__":
    main()
