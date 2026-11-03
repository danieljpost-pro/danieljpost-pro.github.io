#!/usr/bin/env python3
"""Write the default resume view into a page as static HTML.

The page's own JavaScript is the only renderer. This script serves the repository on
127.0.0.1, lets headless Chromium run the page with no URL parameters, and copies the
rendered markup between the page's marker comments back into the source file:

    <!-- HEAD:BEGIN ... -->   ...   <!-- HEAD:END -->      title, description, Open Graph, JSON-LD
    <!-- STATIC:BEGIN ... --> ...   <!-- STATIC:END -->    the resume itself (#app)

Readers that don't run JavaScript (ATS fetchers, AI assistants, link previews) then get
the same default resume a browser shows.

    python3 tools/build_static.py            # update resume-develop.html in place
    python3 tools/build_static.py --check    # exit 1 if the file is out of date
    python3 tools/build_static.py --og-image # also render img/og-sunrise.svg to PNG
"""

import argparse
import functools
import http.server
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKERS = ("HEAD", "STATIC")


def find_chromium() -> str:
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("No Chromium found on PATH. Install it (e.g. `sudo apt install chromium-browser`).")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve():
    handler = functools.partial(QuietHandler, directory=str(ROOT))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def chromium(*args: str) -> subprocess.CompletedProcess:
    cmd = [find_chromium(), "--headless", "--disable-gpu", "--hide-scrollbars", "--no-first-run", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def region(text: str, name: str) -> re.Match:
    pattern = re.compile(rf"(<!-- {name}:BEGIN[^>]*-->)(.*?)([ \t]*<!-- {name}:END -->)", re.S)
    m = pattern.search(text)
    if not m:
        sys.exit(f"Marker pair {name}:BEGIN/{name}:END not found.")
    return m


def build(page: str) -> str:
    source_path = ROOT / page
    source = source_path.read_text(encoding="utf-8")
    server = serve()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/{page}"
        result = chromium("--virtual-time-budget=15000", "--dump-dom", url)
    finally:
        server.shutdown()
    dom = result.stdout
    if 'data-rendered="1"' not in dom:
        sys.exit(f"The page did not finish rendering in Chromium.\n{result.stderr[-2000:]}")

    out = source
    for name in MARKERS:
        rendered = region(dom, name).group(2)
        target = region(out, name)
        out = out[: target.start(2)] + rendered + out[target.end(2):]
    return out


def og_image() -> None:
    svg, png = ROOT / "img" / "og-sunrise.svg", ROOT / "img" / "og-sunrise.png"
    server = serve()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/img/og-sunrise.svg"
        # Snap-packaged Chromium may not write outside its sandbox, so shoot into a temp dir it can reach.
        with tempfile.TemporaryDirectory(dir=Path.home() / "snap" / "chromium" / "common"
                                         if (Path.home() / "snap" / "chromium" / "common").is_dir() else None) as tmp:
            shot = Path(tmp) / "og.png"
            result = chromium("--window-size=1200,627", f"--screenshot={shot}", url)
            if not shot.exists():
                sys.exit(f"Chromium did not write the screenshot.\n{result.stderr[-2000:]}")
            shutil.copyfile(shot, png)
    finally:
        server.shutdown()
    print(f"wrote {png.relative_to(ROOT)} from {svg.relative_to(ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--page", default="resume-develop.html")
    ap.add_argument("--check", action="store_true", help="exit 1 if the page is out of date; write nothing")
    ap.add_argument("--og-image", action="store_true", help="also render img/og-sunrise.svg to PNG")
    args = ap.parse_args()

    if args.og_image:
        og_image()

    path = ROOT / args.page
    current = path.read_text(encoding="utf-8")
    updated = build(args.page)
    if args.check:
        if updated != current:
            print(f"{args.page} is out of date: run tools/build_static.py")
            return 1
        print(f"{args.page} is up to date")
        return 0
    if updated != current:
        path.write_text(updated, encoding="utf-8")
        print(f"updated {args.page}")
    else:
        print(f"{args.page} already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
