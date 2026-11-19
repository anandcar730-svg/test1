#!/usr/bin/env python3
"""Capture a page's design data and screenshots with Playwright.

    python capture.py <url-or-path> --out <dir> [--widths 1440,390] [--prefix ref]
"""
import argparse
import pathlib
import sys

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
EXTRACT_JS = (HERE / "extract_design.js").read_text()


def to_url(target: str) -> str:
    if "://" in target:
        return target
    return pathlib.Path(target).resolve().as_uri()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target")
    ap.add_argument("--out", required=True)
    ap.add_argument("--widths", default="1440,390")
    ap.add_argument("--prefix", default="ref")
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--timeout", type=int, default=45000)
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    url = to_url(args.target)

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path="/opt/pw-browsers/chromium", args=["--no-sandbox"])
        for width in [int(w) for w in args.widths.split(",")]:
            page = browser.new_page(viewport={"width": width, "height": args.height}, device_scale_factor=1)
            try:
                page.goto(url, wait_until="networkidle", timeout=args.timeout)
            except Exception as exc:
                print(f"[warn] {width}px: {exc.__class__.__name__}: continuing with what loaded", file=sys.stderr)
            page.evaluate("""async () => {
                for (let y = 0; y < document.body.scrollHeight; y += innerHeight * 0.8) {
                    scrollTo(0, y); await new Promise(r => setTimeout(r, 120));
                }
                scrollTo(0, 0); await new Promise(r => setTimeout(r, 300));
            }""")
            page.screenshot(path=str(out / f"{args.prefix}-{width}-top.png"))
            page.screenshot(path=str(out / f"{args.prefix}-{width}.png"), full_page=True)
            data = page.evaluate(EXTRACT_JS)
            (out / f"{args.prefix}-{width}.json").write_text(data)
            print(f"[ok] {width}px -> {args.prefix}-{width}.png / .json")
            page.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
