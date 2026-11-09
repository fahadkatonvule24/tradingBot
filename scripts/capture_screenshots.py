"""Regenerate docs/screenshots from real runs of the CLI.

    pip install playwright
    python scripts/capture_screenshots.py --data data            # after `tradebot fetch`
    python scripts/capture_screenshots.py --data data --with-mt5 # also capture `tradebot check`
    python scripts/capture_screenshots.py --dashboard http://127.0.0.1:8765/  # while `tradebot ui` runs

Terminal output is recorded through rich and rendered to PNG; report screenshots are taken from
the HTML the CLI writes. Nothing is mocked up.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import shutil
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from rich.console import Console

import tradebot.cli as cli

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"


def record(argv: list[str], title: str, width: int = 118) -> str:
    """Run the CLI with a recording console and return its output as an SVG terminal window."""
    console = Console(
        record=True, width=width, force_terminal=True, color_system="truecolor", file=io.StringIO()
    )
    console.status = lambda *a, **k: contextlib.nullcontext()  # spinners don't belong in a screenshot
    cli.console = console
    code = cli.main(argv)
    if code != 0:
        raise SystemExit(f"tradebot {' '.join(argv)} exited with {code}")
    return console.export_svg(title=title)


def shoot(page: Page, selectors: list[str], path: Path, pad: int = 16) -> None:
    boxes = [page.locator(s).first.bounding_box() for s in selectors]
    left = max(0, min(b["x"] for b in boxes) - pad)
    top = max(0, min(b["y"] for b in boxes) - pad)
    right = max(b["x"] + b["width"] for b in boxes) + pad
    bottom = max(b["y"] + b["height"] for b in boxes) + pad
    page.screenshot(
        path=str(path),
        full_page=True,
        clip={"x": left, "y": top, "width": right - left, "height": bottom - top},
    )
    print(f"wrote {path.relative_to(ROOT)}")


def capture_dashboard(url: str, browser_name: str) -> None:
    """The live advisor page, exactly as served (needs `tradebot ui` connected to a terminal)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(**({} if browser_name == "chromium" else {"channel": browser_name}))
        for scheme, name, selectors in (
            ("light", "dashboard.png", None),
            ("dark", "dashboard-dark.png", ["header.top", ".row2"]),
        ):
            page = browser.new_page(
                viewport={"width": 1360, "height": 900}, device_scale_factor=1.5, color_scheme=scheme
            )
            page.goto(url)
            page.wait_for_selector(".sym")
            page.wait_for_timeout(3000)  # first chart fetch and a few live ticks
            if selectors:
                shoot(page, selectors, OUT / name, pad=0)
            else:
                page.screenshot(path=str(OUT / name), full_page=True)
                print(f"wrote {(OUT / name).relative_to(ROOT)}")
            page.close()
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=ROOT / "data")
    parser.add_argument("--strategy", default="ema_cross")
    parser.add_argument("--timeframe", default="H1")
    parser.add_argument(
        "--with-mt5", action="store_true", help="also capture `tradebot check` (needs a terminal)"
    )
    parser.add_argument("--browser", default="msedge", help="msedge, chrome, or chromium")
    parser.add_argument("--dashboard", metavar="URL", help="capture a running `tradebot ui` instead")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.dashboard:
        capture_dashboard(args.dashboard, args.browser)
        return
    os.chdir(ROOT)  # relative paths in the output, no local usernames in screenshots
    work = Path("reports")
    data = os.path.relpath(args.data, ROOT)
    terminals = {}
    if args.with_mt5:
        terminals["terminal-check.png"] = record(["check"], "tradebot check")
    common = ["--data", data, "--strategy", args.strategy, "--timeframe", args.timeframe, "-o", str(work)]
    flags = f"--strategy {args.strategy} --timeframe {args.timeframe}"
    terminals["terminal-backtest.png"] = record(["backtest", *common], f"tradebot backtest {flags}")
    terminals["terminal-walkforward.png"] = record(
        ["optimize", *common, "--folds", "4"], f"tradebot optimize {flags} --folds 4"
    )
    report = (work / f"walkforward_{args.strategy}_{args.timeframe}.html").resolve()
    shots = Path(".screenshots-tmp")
    shots.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(**({} if args.browser == "chromium" else {"channel": args.browser}))
        page = browser.new_page(viewport={"width": 1180, "height": 900}, device_scale_factor=2)
        for name, svg in terminals.items():
            html = (shots / (name + ".html")).resolve()
            html.write_text(
                f'<!doctype html><body style="margin:0;background:#fff"><div id="t">{svg}</div>', "utf-8"
            )
            page.goto(html.as_uri())
            shoot(page, ["#t svg"], OUT / name, pad=0)

        page.goto(report.as_uri())
        shoot(page, ["header.top", ".banner", ".kpis", "section.card >> nth=0"], OUT / "report-overview.png")
        shoot(page, ["section.grid-2"], OUT / "report-symbols.png")
        shoot(page, ["section.card >> nth=1", "section.card >> nth=2"], OUT / "report-walkforward.png")
        page.emulate_media(color_scheme="dark")
        page.goto(report.as_uri())
        shoot(page, [".kpis", "section.card >> nth=0"], OUT / "report-dark.png")
        browser.close()
    shutil.rmtree(shots)


if __name__ == "__main__":
    main()
