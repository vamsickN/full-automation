#!/usr/bin/env python
"""Generate 10 images using existing data/images/*.png as style reference.

Uses gpt-image-2 via derouter. Each prompt gets a contact-sheet of N random
existing images labeled "STYLE REF — match this art style" — same mechanism
the app uses for style copying.

Auto-loads DEROUTER_API_KEY from .env if not already in the environment.

Usage:
    python gen_with_refs.py
    python gen_with_refs.py --prompts my_prompts.txt --refs-per-prompt 4
    python gen_with_refs.py --ref-images a.png b.png c.png --count 10
"""
import argparse
import os
import random
import sys
import time

# Auto-load .env so the user doesn't have to export DEROUTER_API_KEY
# (the .env may live in the OTHER copy of continuity-studio, not where the
#  script runs from — the E: drive is the active project, C: is the source)
def _load_env():
    candidates = [
        os.path.join(os.path.dirname(__file__), ".env"),
        os.path.join(os.path.dirname(__file__), "..", ".env"),
        "E:/time now/continuity-studio/continuity-studio/.env",
        "C:/Users/sickv/continuity-studio/.env",
        "C:/Users/sickv/continuity-studio-public/.env",
        "C:/Users/sickv/bulk-gen/.env",
        "C:/Users/sickv/image-to-video/.env",
        os.path.expanduser("~/.env"),
    ]
    for candidate in candidates:
        p = os.path.abspath(candidate)
        if not os.path.exists(p):
            continue
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k and k not in os.environ and v:
                    os.environ[k] = v

_load_env()

# Run from the project dir so the imports below work
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config  # noqa: E402
import pipeline  # noqa: E402
import store  # noqa: E402
from derouter import ImageClient  # noqa: E402
import image_queue  # noqa: E402


DEFAULT_PROMPTS = [
    "Same cartoon web comic style. MAX on the couch scrolling phone, BUDDY lying next to him asleep, evening lamp glow.",
    "Same cartoon web comic style. LILY confidently walking into a party, MAX frozen at the door with BUDDY pulling his hoodie.",
    "Same cartoon web comic style. MAX and LILY back-to-back, MAX sweating, LILY calm, BUDDY between them with a heart speech bubble.",
    "Same cartoon web comic style. Close-up of BUDDY's huge worried eyes looking at the camera, paw raised.",
    "Same cartoon web comic style. MAX alone at a coffee shop, empty chair across, BUDDY under the table hiding.",
    "Same cartoon web comic style. LILY waving from across the street, MAX hiding behind a lamppost with BUDDY peeking out.",
    "Same cartoon web comic style. Split panel: left MAX overthinking with storm cloud head, right LILY chilling with sunshine.",
    "Same cartoon web comic style. MAX, LILY, and BUDDY walking together down a sidewalk, neon city background.",
    "Same cartoon web comic style. BUDDY dragging MAX by the hoodie towards LILY, motion lines, MAX's legs off the ground.",
    "Same cartoon web comic style. Cozy bedroom, MAX under blanket, BUDDY on pillow, LILY's text message floating above phone screen.",
]


def pick_refs(image_dir, n, exclude=None):
    """Pick n random PNGs from image_dir, optionally excluding any whose path
    matches the exclude set (e.g. ones we just generated)."""
    files = [f for f in os.listdir(image_dir) if f.lower().endswith(".png")]
    if exclude:
        files = [f for f in files if os.path.join(image_dir, f) not in exclude]
    if not files:
        raise SystemExit(f"no PNGs found in {image_dir}")
    random.shuffle(files)
    picked = files[:n]
    paths = [os.path.join(image_dir, f) for f in picked]
    return picked, paths


def load_ref_bytes(paths):
    out = []
    for p in paths:
        with open(p, "rb") as f:
            out.append(f.read())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref-dir", default=os.path.join(HERE, "data", "images"))
    ap.add_argument("--refs-per-prompt", type=int, default=4,
                    help="how many reference images to attach per generation")
    ap.add_argument("--ref-images", nargs="*", default=None,
                    help="explicit reference image paths; if set, use these "
                         "instead of random picks from --ref-dir")
    ap.add_argument("--prompts", default=None,
                    help="path to a text file with one prompt per line; "
                         "default uses the built-in 10 prompts")
    ap.add_argument("--count", type=int, default=10,
                    help="how many images to generate (uses first N prompts)")
    ap.add_argument("--out", default=os.path.join(HERE, "data", "images"),
                    help="output directory for generated images")
    ap.add_argument("--size", default=config.DEFAULT_SIZE)
    ap.add_argument("--quality", default=config.DEFAULT_QUALITY)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    if not config.API_KEY:
        raise SystemExit(
            "DEROUTER_API_KEY not set. export it before running:\n"
            "  export DEROUTER_API_KEY=sk-...\n"
            "  python gen_with_refs.py"
        )

    os.makedirs(args.out, exist_ok=True)

    # 1. prompts
    if args.prompts:
        with open(args.prompts, "r", encoding="utf-8") as f:
            prompts = [l.strip() for l in f if l.strip()]
    else:
        prompts = DEFAULT_PROMPTS
    prompts = prompts[: args.count]

    # 2. refs
    if args.ref_images:
        ref_paths = [os.path.abspath(p) for p in args.ref_images]
        for p in ref_paths:
            if not os.path.exists(p):
                raise SystemExit(f"ref image not found: {p}")
    else:
        _, ref_paths = pick_refs(args.ref_dir, args.refs_per_prompt)
    ref_bytes = load_ref_bytes(ref_paths)
    ref_labels = ["STYLE REF — match this art style"] * len(ref_bytes)
    contact = pipeline.contact_sheet(ref_bytes, labels=ref_labels)

    print(f"[gen] {len(prompts)} prompts x model={config.MODEL} size={args.size} "
          f"quality={args.quality} refs={len(ref_paths)}", file=sys.stderr)

    client = ImageClient()

    # 3. generate
    for i, prompt in enumerate(prompts, 1):
        ts = int(time.time())
        out_name = f"refgen_{ts}_{i:02d}.png"
        out_path = os.path.join(args.out, out_name)
        full = (f"You are rendering ONE frame in the same art style as the "
                f"attached STYLE REF images. Copy their rendering technique, "
                f"line work, palette, shading and proportions exactly. Do NOT "
                f"drift toward a more detailed or realistic style.\n\n"
                f"NEW FRAME:\n{prompt.strip()}")
        print(f"\n[{i}/{len(prompts)}] {prompt[:80]}...", file=sys.stderr)
        t0 = time.time()
        try:
            img_bytes = image_queue.run_with_retry(
                lambda: client.edit(full, [contact],
                                    size=args.size, quality=args.quality),
                index=i, model=config.MODEL, label="refgen",
            )
        except image_queue.ImageError as e:
            print(f"  FAILED: {e}", file=sys.stderr)
            continue
        with open(out_path, "wb") as f:
            f.write(img_bytes)
        print(f"  -> {out_path}  ({round(time.time()-t0,1)}s, "
              f"{len(img_bytes)//1024} KB)", file=sys.stderr)

    print(f"\n[gen] done. {len(prompts)} prompts processed, output in {args.out}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
