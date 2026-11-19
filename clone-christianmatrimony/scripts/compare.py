#!/usr/bin/env python3
"""Compare a reference screenshot with a clone screenshot.

    python compare.py ref.png clone.png --out compare-1440.png
"""
import argparse
import sys

import numpy as np
from PIL import Image


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reference")
    ap.add_argument("clone")
    ap.add_argument("--out", required=True)
    ap.add_argument("--band", type=int, default=200, help="band height in px for the per-region report")
    args = ap.parse_args()

    ref = Image.open(args.reference).convert("RGB")
    cln = Image.open(args.clone).convert("RGB")
    w = ref.width
    if cln.width != w:
        cln = cln.resize((w, round(cln.height * w / cln.width)))
    h = max(ref.height, cln.height)

    def pad(img):
        canvas = Image.new("RGB", (w, h), (255, 0, 255))
        canvas.paste(img, (0, 0))
        return canvas

    a = np.asarray(pad(ref)).astype(np.int16)
    b = np.asarray(pad(cln)).astype(np.int16)
    diff = np.abs(a - b).max(axis=2)
    mismatch = diff > 40
    score = mismatch.mean() * 100

    heat = np.zeros((h, w, 3), dtype=np.uint8)
    heat[..., 0] = np.clip(diff * 3, 0, 255)
    gray = (np.asarray(pad(ref).convert("L")) * 0.25).astype(np.uint8)
    heat[..., 1] = np.where(mismatch, 0, gray)
    heat[..., 2] = np.where(mismatch, 0, gray)

    panel = Image.new("RGB", (w * 3 + 20, h), (255, 255, 255))
    panel.paste(pad(ref), (0, 0))
    panel.paste(pad(cln), (w + 10, 0))
    panel.paste(Image.fromarray(heat), (2 * w + 20, 0))
    if panel.width > 3000:
        panel = panel.resize((3000, round(panel.height * 3000 / panel.width)))
    panel.save(args.out)

    print(f"reference {ref.width}x{ref.height}, clone {cln.width}x{cln.height} (scaled)")
    gap = abs(ref.height - cln.height) / ref.height * 100
    if gap > 5:
        print(f"height differs by {gap:.1f}% — check for missing sections or spacing drift")
    print(f"pixel mismatch: {score:.1f}%")
    bands = []
    for top in range(0, h, args.band):
        bands.append((mismatch[top: top + args.band].mean() * 100, top))
    print("worst bands (y-range: mismatch%):")
    for pct, top in sorted(bands, reverse=True)[:5]:
        print(f"  {top}-{min(top + args.band, h)}px: {pct:.0f}%")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
