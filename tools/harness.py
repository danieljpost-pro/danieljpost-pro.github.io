#!/usr/bin/env python3
"""ATS-readability harness for the resume page (Step 2 of the ATS plan).

For each URL configuration (default, every tier, the client profiles, anonymized data,
maximum sliders) the harness serves the repository on 127.0.0.1, drives headless Chromium
over the DevTools pipe (no Node.js, no websocket library) and captures:

    raw HTML (urllib, no JS) · DOM after JS · a PDF printed the way Chrome's dialog does it ·
    pdftotext default/-layout/-raw · pdfinfo/pdffonts/struct tree/outline · axe-core in light
    and dark · the print-media accessibility tree · Readability · desktop/phone screenshots

and runs checks H1–H21 from `reports/ATS page usability 2026-11.md` in the Work vault.
Each configuration gets `harness-out/<config>/digest.md` (what the Step 3 agents read);
`harness-out/summary.md` is the matrix. Exit status 1 if any check FAILs.

    python3 tools/harness.py                  # every configuration
    python3 tools/harness.py --only default anon client-optum
    python3 tools/harness.py --list           # show configuration names

Optional extractors (pdfminer.six, pypdf, PyMuPDF, Trafilatura) are used when importable and
reported as SKIP otherwise. To get them without touching the system Python:

    python3 -m venv ~/.venvs/resume-harness
    ~/.venvs/resume-harness/bin/pip install pdfminer.six pypdf pymupdf trafilatura
    ~/.venvs/resume-harness/bin/python tools/harness.py

Analytics requests are blocked inside the harness browser so runs never reach GA.
"""

import argparse
import base64
import difflib
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zlib
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_static import ROOT, find_chromium, serve  # noqa: E402

PAGE = "resume-develop.html"
SITE = "https://danieljpost.pro/"
OUT = ROOT / "harness-out"
CACHE = Path(__file__).resolve().parent / ".cache"
VENDOR = {   # pinned; fetched once into tools/.cache and verified by hash
    "axe": ("https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js",
            "b511cd9dec01c76f4b2ad1723b66b6db37d4c2eb4ed199076e1829d9ee7b75e3"),
    "readability": ("https://cdn.jsdelivr.net/npm/@mozilla/readability@0.5.0/Readability.js",
                    "61521a78d29e60def9623a14ebcc5a54aff88c61fd11326d7c016478a4f23047"),
}
BLOCKED = ["*googletagmanager.com*", "*google-analytics.com*", "*doubleclick.net*"]
HEADINGS = ["Summary", "Experience", "Skills", "Recommendations"]
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


# ---------------------------------------------------------------- DevTools over a pipe

class CDPError(RuntimeError):
    pass


class Browser:
    """Headless Chromium speaking the DevTools protocol on fds 3/4 (--remote-debugging-pipe)."""

    def __init__(self):
        snap = Path.home() / "snap" / "chromium" / "common"   # snap Chromium can only write here
        self.profile = tempfile.mkdtemp(prefix="harness-", dir=snap if snap.is_dir() else None)
        to_child_r, self._w = os.pipe()
        self._r, from_child_w = os.pipe()
        hi_r = fcntl.fcntl(to_child_r, fcntl.F_DUPFD, 10)
        hi_w = fcntl.fcntl(from_child_w, fcntl.F_DUPFD, 10)

        def child():
            os.dup2(hi_r, 3)
            os.dup2(hi_w, 4)

        self.proc = subprocess.Popen(
            [find_chromium(), "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             "--hide-scrollbars", "--mute-audio", "--remote-debugging-pipe", f"--user-data-dir={self.profile}",
             "about:blank"],
            pass_fds=(3, 4), preexec_fn=child,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for fd in (to_child_r, from_child_w, hi_r, hi_w):
            os.close(fd)
        self._buf = b""
        self._id = 0
        self.events = []

    def _message(self, deadline):
        while b"\0" not in self._buf:
            left = deadline - time.time()
            if left <= 0 or not select.select([self._r], [], [], left)[0]:
                raise CDPError("timed out waiting for Chromium")
            chunk = os.read(self._r, 1 << 20)
            if not chunk:
                raise CDPError("Chromium closed the DevTools pipe")
            self._buf += chunk
        raw, self._buf = self._buf.split(b"\0", 1)
        return json.loads(raw)

    def send(self, method, params=None, session=None, timeout=60):
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        data = json.dumps(msg).encode() + b"\0"
        while data:
            data = data[os.write(self._w, data):]
        deadline = time.time() + timeout
        while True:
            m = self._message(deadline)
            if m.get("id") == self._id:
                if "error" in m:
                    raise CDPError(f"{method}: {m['error'].get('message')}")
                return m.get("result", {})
            self.events.append(m)

    def tab(self):
        target = self.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.send("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        return Tab(self, target, session)

    def close(self):
        try:
            self.send("Browser.close", timeout=5)
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


class Tab:
    def __init__(self, browser, target, session):
        self.b, self.target, self.s = browser, target, session
        for domain in ("Page", "Runtime", "Network"):
            self.send(f"{domain}.enable")
        self.send("Network.setBlockedURLs", {"urls": BLOCKED})
        self.send("Network.setCacheDisabled", {"cacheDisabled": True})

    def send(self, method, params=None, timeout=60):
        return self.b.send(method, params, self.s, timeout)

    def eval(self, expr, await_promise=False, timeout=60):
        r = self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": await_promise}, timeout)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise CDPError(f"JS error: {d.get('exception', {}).get('description') or d.get('text')}")
        return r["result"].get("value")

    def viewport(self, width, height, mobile=False, scale=1):
        self.send("Emulation.setDeviceMetricsOverride",
                  {"width": width, "height": height, "deviceScaleFactor": scale, "mobile": mobile})

    def media(self, media="", scheme="light"):
        self.send("Emulation.setEmulatedMedia",
                  {"media": media, "features": [{"name": "prefers-color-scheme", "value": scheme}]})

    def goto(self, url, timeout=20):
        self.b.events.clear()
        self.send("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval("document.readyState === 'complete' && document.documentElement.dataset.rendered === '1'"):
                    self.eval("document.fonts.ready.then(() => true)", await_promise=True)
                    return True
            except CDPError:
                pass   # navigation in progress
            time.sleep(0.1)
        return False

    def screenshot(self, path, full=False):
        r = self.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": full})
        path.write_bytes(base64.b64decode(r["data"]))

    def pdf(self, margin_in):
        # The dialog's "Default" margins are the page's @page margins; printToPDF would otherwise
        # impose its own 0.4in. Its header/footer templates are drawn even at margin 0, unlike the
        # dialog's, so header/footer leakage (H4) is tested separately with cli_print().
        m = {f"margin{side}": margin_in for side in ("Top", "Bottom", "Left", "Right")}
        r = self.send("Page.printToPDF", {
            "displayHeaderFooter": False,
            "printBackground": False,           # the dialog's default: "Background graphics" off
            "preferCSSPageSize": True, "generateTaggedPDF": True, "generateDocumentOutline": True, **m,
        }, timeout=120)
        return base64.b64decode(r["data"])

    def close(self):
        try:
            self.b.send("Target.closeTarget", {"targetId": self.target})
        except CDPError:
            pass


def cli_print(url, dest):
    """Print with `chromium --print-to-pdf` and Chrome's default header/footer left on, as a user
    would with the dialog's "Headers and footers" box ticked. Analytics hosts don't resolve."""
    snap = Path.home() / "snap" / "chromium" / "common"
    with tempfile.TemporaryDirectory(prefix="harness-cli-", dir=snap if snap.is_dir() else None) as tmp:
        pdf = Path(tmp) / "out.pdf"
        rules = ", ".join(f"MAP {h} ~NOTFOUND" for h in ("*.googletagmanager.com", "*.google-analytics.com", "*.doubleclick.net"))
        subprocess.run([find_chromium(), "--headless", "--disable-gpu", "--no-first-run", f"--user-data-dir={tmp}/profile",
                        f"--host-resolver-rules={rules}", "--virtual-time-budget=15000", f"--print-to-pdf={pdf}", url],
                       capture_output=True, timeout=120)
        if not pdf.is_file():
            return None
        shutil.copyfile(pdf, dest)
    return dest


# ---------------------------------------------------------------- page knowledge

def page_semantics(source):
    """Slider definitions and step meanings, read from the page's own JS so they can't drift."""
    sliders = {}
    for m in re.finditer(r"key:\s*'(\w+)',\s*label:\s*'[^']*',\s*min:\s*(\d+),\s*max:\s*(\d+),\s*def:\s*(\d+)", source):
        sliders[m[1]] = {"min": int(m[2]), "max": int(m[3]), "def": int(m[4])}

    def mapping(name):
        m = re.search(rf"const {name}\s*=\s*\{{([^}}]*)\}}", source)
        if not m:
            return {}
        return {int(k): (float("inf") if v == "Infinity" else int(v))
                for k, v in re.findall(r"(\d+):\s*(\w+)", m[1])}

    default_tier = re.search(r"const DEFAULT_TIER\s*=\s*'([^']+)'", source)[1]
    return {"sliders": sliders, "default_tier": default_tier,
            "accomplishments": mapping("ACCOMPLISHMENTS"), "skill_count": mapping("SKILL_COUNT"),
            "technologies": mapping("TECHNOLOGIES")}


def configurations(data, clients, sem):
    configs = [{"name": "default", "query": {}, "default_sliders": True}]
    for tier in data.get("tiers", []):
        configs.append({"name": "tier-" + slugify(tier).lower(), "query": {"tier": tier}, "default_sliders": True})
    for cid in clients:
        configs.append({"name": f"client-{cid}", "query": {"clientid": cid}, "default_sliders": None})
    configs.append({"name": "anon", "query": {"data": "anon"}, "default_sliders": True})
    configs.append({"name": "max", "query": {k: v["max"] for k, v in sem["sliders"].items()}, "default_sliders": False})
    return configs


def effective(cfg, data, clients, sem):
    """The tier and slider values the page should end up with, per its documented rules."""
    q = cfg["query"]
    profile = clients.get(str(q.get("clientid", "")).lower()) if "clientid" in q else None
    src = profile if profile else q
    tiers = data.get("tiers", [])
    match = lambda t: next((x for x in tiers if x.lower() == str(t or "").strip().lower()), None)
    out = {"tier": match(src.get("tier")) or match(sem["default_tier"]) or tiers[0], "locked": bool(profile),
           "client": profile.get("name") if profile else None}
    for key, s in sem["sliders"].items():
        v = src.get(key)
        try:
            v = round(float(v))
        except (TypeError, ValueError):
            v = s["def"]
        out[key] = min(s["max"], max(s["min"], v))
    if cfg["default_sliders"] is None:
        cfg["default_sliders"] = all(out[k] == s["def"] for k, s in sem["sliders"].items())
    return out


def css_inches(length, fallback=0.4):
    """A single CSS length (the @page margin shorthand's first value) in inches."""
    m = re.match(r"\s*([\d.]+)\s*(px|in|cm|mm|pt)?", length or "")
    if not m:
        return fallback   # no @page margin: Chrome's default
    per_inch = {"px": 96, "in": 1, "cm": 2.54, "mm": 25.4, "pt": 72, None: 96}
    return float(m[1]) / per_inch[m[2]]


def slugify(s):
    return re.sub(r"^-+|-+$", "", re.sub(r"[^A-Za-z0-9]+", "-", str(s or "")))


def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def words(s):
    return re.findall(r"[a-z0-9]+", (s or "").lower())


JS_FACTS = r"""(() => {
  const t = el => (el?.textContent ?? '').replace(/\s+/g, ' ').trim();
  const positions = [...document.querySelectorAll('#app .job')].map(el => {
    if (el.classList.contains('compact')) {
      const [title, company, range] = t(el).split(' | ');
      return { kind: 'compact', title, company, range };
    }
    const head = t(el.querySelector(':scope > .job-title'));
    const parts = t(el.querySelector(':scope > .job-meta')).split(' | ');
    const range = (parts[parts.length - 1] ?? '').replace(/\s*\(.*\)\s*$/, '');
    const wins = el.querySelectorAll(':scope > ul > li').length;
    return el.classList.contains('firm')
      ? { kind: 'firm', title: parts[0], company: head, range, wins }
      : { kind: 'full', title: head, company: parts[0], range, wins };
  });
  const meta = sel => document.querySelector(sel)?.getAttribute('content') ?? null;
  return {
    title: document.title,
    h1: [...document.querySelectorAll('h1')].map(t),
    h2: [...document.querySelectorAll('#app h2')].map(t),
    positions,
    skills: [...document.querySelectorAll('#app .skills li')].map(li => t(li)),
    recs: document.querySelectorAll('#app .rec').length,
    prepared: t(document.querySelector('#app .prepared')) || null,
    contact: t(document.querySelector('#app .contact')),
    appText: document.getElementById('app')?.innerText ?? '',
    robots: meta('meta[name="robots"]'),
    canonical: document.querySelector('link[rel="canonical"]')?.getAttribute('href') ?? null,
    ld: document.getElementById('ld')?.textContent ?? null,
    images: document.querySelectorAll('#app img, #app svg, #app picture, #app meter, #app progress, #app canvas').length,
    controls: document.querySelectorAll('#controls input, #controls button').length,
    tailoring: document.querySelectorAll('#controls input').length,
    overflowX: document.documentElement.scrollWidth > innerWidth + 1,
  };
})()"""

JS_HIDDEN = r"""(() => {
  const parse = c => (c.match(/[\d.]+/g) ?? []).map(Number);
  const lum = ([r, g, b]) => {
    const f = v => (v /= 255) <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const bg = el => {
    for (; el; el = el.parentElement) {
      const c = parse(getComputedStyle(el).backgroundColor);
      if (c.length === 3 || (c.length === 4 && c[3] > 0.5)) return c.slice(0, 3);
    }
    return [255, 255, 255];
  };
  const out = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const n = w.currentNode, text = n.textContent.replace(/\s+/g, ' ').trim();
    const el = n.parentElement;
    if (!text || !el || el.closest('script, style, template, noscript, head')) continue;
    const cs = getComputedStyle(el), why = [];
    if (!el.checkVisibility({ opacityProperty: true, visibilityProperty: true })) why.push('not rendered/visible');
    const r = document.createRange(); r.selectNodeContents(n);
    const box = r.getBoundingClientRect();
    if (!why.length && (box.width < 2 || box.height < 2)) why.push('clipped to <2px');
    if (box.right < 0 || box.bottom < -10 + -scrollY) why.push('off-screen');
    if (parseFloat(cs.fontSize) < 6) why.push(`font-size ${cs.fontSize}`);
    const fg = parse(cs.color);
    if (!why.length && fg.length >= 3 && !(fg.length === 4 && fg[3] < 0.05)) {
      const [a, b] = [lum(fg), lum(bg(el))].sort((x, y) => y - x);
      const ratio = (a + 0.05) / (b + 0.05);
      if (ratio < 1.5) why.push(`contrast ${ratio.toFixed(2)}:1`);
    }
    if (why.length) out.push({ text: text.slice(0, 90), in: el.closest('#app') ? 'app' : (el.closest('[id]')?.id ?? 'body'), why });
  }
  return out;
})()"""

JS_PRINT_LINT = r"""(() => {
  const sel = el => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + [...el.classList].map(c => '.' + c).join('');
  const hasText = el => [...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
  const found = { grid: [], flex: [], columns: [], float: [], letterSpacing: [], ligatures: [], rightAligned: [], textTransform: [] };
  const seen = new Set();
  const add = (k, v) => { const key = k + v; if (!seen.has(key)) { seen.add(key); found[k].push(v); } };
  for (const el of document.querySelectorAll('#app, #app *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none') continue;
    if (/grid/.test(cs.display)) add('grid', `${sel(el)} (${cs.gridTemplateColumns.split(' ').length} column track${cs.gridTemplateColumns.split(' ').length > 1 ? 's' : ''})`);
    if (/flex/.test(cs.display)) add('flex', sel(el));
    if (!['auto', '1'].includes(cs.columnCount)) add('columns', sel(el));
    if (cs.float !== 'none') add('float', sel(el));
    if (cs.letterSpacing !== 'normal' && parseFloat(cs.letterSpacing) > 0.1 * parseFloat(cs.fontSize)) add('letterSpacing', sel(el));
    if (hasText(el) && cs.fontVariantLigatures !== 'none') add('ligatures', `${sel(el)} (${cs.fontVariantLigatures})`);
    if (el.matches('.job-meta, .job.compact') && /right|end/.test(cs.textAlign)) add('rightAligned', sel(el));
    if (hasText(el) && cs.textTransform !== 'none') add('textTransform', `${sel(el)} (${cs.textTransform})`);
  }
  const breaks = {};
  const want = { '.job p': ['breakInside', 'avoid'], '.job li': ['breakInside', 'avoid'], '.job.compact': ['breakInside', 'avoid'],
                 '.rec': ['breakInside', 'avoid'], '.job-title': ['breakAfter', 'avoid'], '.job-meta': ['breakAfter', 'avoid'],
                 '#app h2': ['breakAfter', 'avoid'] };
  for (const [s, [prop, val]] of Object.entries(want)) {
    const els = [...document.querySelectorAll(s)];
    breaks[s] = els.length ? (els.every(e => getComputedStyle(e)[prop] === val) ? 'ok' : `not ${prop}: ${val}`) : 'absent';
  }
  let pageMargin = null;
  for (const sheet of document.styleSheets) {
    try { for (const r of sheet.cssRules) if (r instanceof CSSPageRule) pageMargin = r.style.margin || pageMargin; } catch {}
  }
  const links = [...document.querySelectorAll('#app a')].map(a => ({ text: a.textContent.trim(), href: a.getAttribute('href') }));
  const hiddenInPrint = [...document.querySelectorAll('#app *')].filter(e => getComputedStyle(e).display === 'none' && e.textContent.trim()).map(sel);
  return { found, breaks, pageMargin, links, hiddenInPrint };
})()"""


# ---------------------------------------------------------------- PDF tools

def run(*cmd, check=False):
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"{cmd[0]} failed: {r.stderr[-500:]}")
    return r


def pdf_info(path):
    info = {}
    for line in run("pdfinfo", str(path)).stdout.splitlines():
        k, _, v = line.partition(":")
        info[k.strip()] = v.strip()
    return info


def pdf_fonts(path):
    fonts = []
    for line in run("pdffonts", str(path)).stdout.splitlines()[2:]:
        m = re.match(r"^(\S+)\s+(.+?)\s+(\S+)\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$", line)
        if m:
            fonts.append({"name": m[1], "type": m[2], "encoding": m[3], "emb": m[4] == "yes", "uni": m[6] == "yes"})
    return fonts


def extractors(path):
    """Every text extraction we can run here. Value None means the extractor isn't installed."""
    out = {}
    for mode in ("", "-layout", "-raw"):
        args = ["pdftotext", "-enc", "UTF-8"] + ([mode] if mode else []) + [str(path), "-"]
        out["pdftotext" + mode] = run(*args, check=True).stdout
    try:
        from pdfminer.high_level import extract_text
        out["pdfminer"] = extract_text(str(path))
    except ImportError:
        out["pdfminer"] = None
    try:
        from pypdf import PdfReader
        out["pypdf"] = "\f".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    except ImportError:
        out["pypdf"] = None
    try:
        import fitz
        with fitz.open(str(path)) as doc:
            out["pymupdf"] = "\f".join(p.get_text() for p in doc)
    except ImportError:
        out["pymupdf"] = None
    return out


def struct_text(path):
    r = run("pdfinfo", "-struct-text", str(path))
    texts = re.findall(r'^\s*"(.*)"\s*$', r.stdout, re.M)
    types = re.findall(r"^\s*([A-Za-z0-9]+) \((?:block|inline)\)", r.stdout, re.M)
    errors = sorted(set(re.findall(r"Syntax Error: (.*)", r.stderr)))
    return {"text": " ".join(texts), "types": types, "errors": errors}


def outline(path):
    xml = run("pdftohtml", "-xml", "-i", "-stdout", str(path)).stdout
    return [(int(p), _unxml(t)) for p, t in re.findall(r'<item page="(\d+)">(.*?)</item>', xml)]


def _unxml(s):
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")


def raw_struct(pdf_bytes):
    tags = re.findall(rb"/S\s*/([A-Za-z0-9]+)", pdf_bytes)
    figures = [m for m in re.finditer(rb"/S\s*/Figure\b", pdf_bytes)]
    figures_alt = sum(1 for m in figures if b"/Alt" in pdf_bytes[max(0, m.start() - 400): m.end() + 400])
    return {"tags": sorted(set(t.decode() for t in tags)), "aside": tags.count(b"Aside"),
            "figures": len(figures), "figures_with_alt": figures_alt,
            "objstm": b"/ObjStm" in pdf_bytes}


def content_ops(pdf_bytes):
    """Walk every page content stream: text shown outside marked content, invisible or white text."""
    stats = {"streams": 0, "text_ops": 0, "outside_marked": 0, "white_text": 0, "invisible_text": 0, "tiny_text": 0}
    for m in re.finditer(rb"stream\r?\n", pdf_bytes):
        head = pdf_bytes[max(0, m.start() - 300): m.start()]
        if b"FlateDecode" not in head.split(b"obj")[-1]:
            continue
        try:
            data = zlib.decompressobj().decompress(pdf_bytes[m.end(): m.end() + 5_000_000])
        except zlib.error:
            continue
        if b"BT" not in data or not (b"Tj" in data or b"TJ" in data):
            continue
        sample = data[:4000]
        if sum(32 <= c < 127 or c in (9, 10, 13) for c in sample) < 0.9 * len(sample):
            continue   # a font program, not a content stream
        stats["streams"] += 1
        _scan_ops(data, stats)
    return stats


_TOKEN = re.compile(rb"\((?:\\.|[^\\)])*\)|<<|>>|<[0-9A-Fa-f\s]*>|\[|\]|/[^\s/\[\]()<>{}%]+|%[^\r\n]*|[^\s/\[\]()<>{}%]+")


def _scan_ops(data, stats):
    depth, operands, fill, render, size = 0, [], (0.0,), 0, 12.0
    # Nested parentheses in strings are rare in Chrome output; the token regex treats them as flat.
    for tok in _TOKEN.findall(data):
        if tok[:1] in b"(<[]/" or tok in (b"<<", b">>") or re.fullmatch(rb"[-+.\d]+", tok):
            operands.append(tok)
            continue
        if tok.startswith(b"%"):
            continue
        op = tok
        if op in (b"BDC", b"BMC"):
            depth += 1
        elif op == b"EMC":
            depth = max(0, depth - 1)
        elif op in (b"g", b"rg", b"k", b"sc", b"scn"):
            try:
                fill = tuple(float(x) for x in operands if re.fullmatch(rb"[-+.\d]+", x))
            except ValueError:
                pass
        elif op == b"Tr" and operands:
            render = int(float(operands[-1]))
        elif op == b"Tf" and operands:
            try:
                size = float(operands[-1])
            except ValueError:
                pass
        elif op in (b"Tj", b"TJ", b"'", b'"'):
            stats["text_ops"] += 1
            if depth == 0:
                stats["outside_marked"] += 1
            white = (len(fill) in (1, 3) and all(v >= 0.97 for v in fill)) or (len(fill) == 4 and all(v <= 0.03 for v in fill))
            if white:
                stats["white_text"] += 1
            if render == 3:
                stats["invisible_text"] += 1
            if 0 < abs(size) < 4:
                stats["tiny_text"] += 1
        operands = []


# ---------------------------------------------------------------- checks

class Result:
    def __init__(self):
        self.items = []

    def add(self, cid, status, title, detail=""):
        self.items.append({"id": cid, "status": status, "title": title, "detail": detail})

    def worst(self, cid):
        order = [FAIL, WARN, PASS, SKIP]
        statuses = [i["status"] for i in self.items if i["id"] == cid]
        return min(statuses, key=order.index) if statuses else "—"


def lines_with_pages(text):
    """Non-blank lines and the page each is on. Blank lines don't separate anything for a parser."""
    lines, pages, page = [], [], 1
    for raw in text.split("\n"):
        page += raw.count("\f")
        line = raw.replace("\f", "")
        if line.strip():
            lines.append(line)
            pages.append(page)
    return lines, pages


def locate_positions(text, positions):
    """Find each position in reading order: its start date line, with title, company and end within ±1 line."""
    lines, pages = lines_with_pages(text)
    cursor, found = 0, []
    for p in positions:
        start, _, end = (p["range"] or "").partition(" – ")
        hit = None
        for d in range(cursor, len(lines)):
            if start not in lines[d]:
                continue
            window = norm(" ".join(lines[max(0, d - 1): d + 2]))
            if all(norm(x) in window for x in (p["title"], p["company"], end)) and window.find(start) < window.rfind(end):
                tline = next((i for i in range(max(0, d - 1), d + 1) if norm(p["title"]) in norm(lines[i])), max(0, d - 1))
                hit = {"date_line": d, "title_line": tline, "same_page": pages[tline] == pages[d], "page": pages[d]}
                break
        found.append(hit)
        if hit:
            cursor = hit["date_line"] + 1
    return found


def nonempty_lines(text):
    return [norm(l.replace("\f", "")) for l in text.split("\n") if l.strip(" \f")]


def run_config(browser, port, cfg, ctx):
    res = Result()
    name, data, sem = cfg["name"], ctx["datasets"][cfg["query"].get("data", "default")], ctx["sem"]
    eff = effective(cfg, data, ctx["clients"], sem)
    query = urllib.parse.urlencode(cfg["query"])
    url = f"http://127.0.0.1:{port}/{PAGE}" + (f"?{query}" if query else "")
    out = OUT / name
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    digest = {"name": name, "url": f"/{PAGE}" + (f"?{query}" if query else ""), "effective": eff, "res": res}

    # Raw HTML, no JS
    raw_html = urllib.request.urlopen(url).read()
    digest["raw_sha"] = hashlib.sha256(raw_html).hexdigest()
    (out / "raw.html").write_bytes(raw_html)

    tab = browser.tab()
    try:
        tab.viewport(1280, 900)
        tab.media("", "light")
        if not tab.goto(url):
            res.add("H0", FAIL, "Page renders", "data-rendered never became 1 within 20 s")
            digest["facts"] = None
            return digest
        events = list(browser.events)
        errors = [e["params"]["exceptionDetails"].get("exception", {}).get("description", e["params"]["exceptionDetails"].get("text"))
                  for e in events if e.get("method") == "Runtime.exceptionThrown"]
        console = [" ".join(str(a.get("value", a.get("description", ""))) for a in e["params"].get("args", []))
                   for e in events if e.get("method") == "Runtime.consoleAPICalled" and e["params"].get("type") in ("error", "warning")]
        reached = [e["params"]["request"]["url"] for e in events if e.get("method") == "Network.requestWillBeSent"
                   and any(h in e["params"]["request"]["url"] for h in ("googletagmanager", "google-analytics"))]
        blocked = sum(1 for e in events if e.get("method") == "Network.loadingFailed" and e["params"].get("blockedReason"))
        res.add("H0", FAIL if errors else (WARN if console else PASS), "Page renders without JS errors",
                "; ".join(errors + console) or f"rendered; {blocked} analytics request(s) blocked")
        if reached and blocked < len(reached):
            res.add("H0", FAIL, "Analytics blocked in harness", f"{len(reached) - blocked} request(s) not blocked")

        facts = tab.eval(JS_FACTS)
        digest["facts"] = facts
        (out / "dom.html").write_text(tab.eval("document.documentElement.outerHTML"), encoding="utf-8")
        tab.screenshot(out / "desktop.png")
        hidden = tab.eval(JS_HIDDEN)

        # Readability, in the page
        tab.eval(ctx["vendor"]["readability"] + "\n;true")
        readable = tab.eval("(() => { const a = new Readability(document.cloneNode(true)).parse(); "
                            "return a && { title: a.title, text: a.textContent, length: a.length }; })()")

        # axe-core, light then dark
        tab.eval(ctx["vendor"]["axe"] + "\n;true")
        axe = {}
        for scheme in ("light", "dark"):
            tab.media("", scheme)
            tab.eval("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))", await_promise=True)
            axe[scheme] = tab.eval("axe.run(document, { resultTypes: ['violations'] }).then(r => r.violations.map(v => ("
                                   "{ id: v.id, impact: v.impact, tags: v.tags, help: v.help, "
                                   "nodes: v.nodes.length, targets: v.nodes.slice(0, 3).map(n => n.target.join(' ')) })))",
                                   await_promise=True)
        tab.media("", "light")

        # Print media: lint and accessibility tree
        tab.media("print", "light")
        lint = tab.eval(JS_PRINT_LINT)
        tab.send("Accessibility.enable")
        ax_nodes = tab.send("Accessibility.getFullAXTree")["nodes"]
        tab.media("", "light")

        # The PDF, printed as the dialog would (background graphics off, Chrome header/footer on)
        title_before = tab.eval("document.title")
        pdf_bytes = tab.pdf(css_inches(lint["pageMargin"]))
        title_after = tab.eval("document.title")
        pdf_path = out / "resume.pdf"
        pdf_path.write_bytes(pdf_bytes)

        # Phone
        tab.viewport(390, 844, mobile=True, scale=2)
        tab.eval("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))", await_promise=True)
        phone_overflow = tab.eval("document.documentElement.scrollWidth > innerWidth + 1")
        tab.screenshot(out / "phone.png")
    finally:
        tab.close()

    run("pdftoppm", "-png", "-r", "70", "-f", "1", "-l", "1", "-singlefile", str(pdf_path), str(out / "pdf-page1"))
    info, fonts = pdf_info(pdf_path), pdf_fonts(pdf_path)
    texts = extractors(pdf_path)
    for k, v in texts.items():
        if v is not None:
            (out / f"text-{k}.txt").write_text(v, encoding="utf-8")
    stext, ostruct, oline, ops = struct_text(pdf_path), raw_struct(pdf_bytes), outline(pdf_path), content_ops(pdf_bytes)
    images = len(run("pdfimages", "-list", str(pdf_path)).stdout.splitlines()[2:])
    main_text = texts["pdftotext"]
    pages = int(info.get("Pages", 0))
    digest.update(info=info, fonts=fonts, struct=stext, raw_struct=ostruct, outline=oline, ops=ops, axe=axe,
                  lint=lint, hidden=hidden, readable=readable, title_before=title_before, title_after=title_after,
                  phone_overflow=phone_overflow, images=images)
    positions = facts["positions"]

    # H1 PDF basics
    size = pdf_path.stat().st_size
    page_limit = 2 if cfg["default_sliders"] else None
    h1 = []
    if info.get("Tagged") != "yes":
        h1.append("not tagged")
    if page_limit and pages > page_limit:
        h1.append(f"{pages} pages (limit {page_limit} at default sliders)")
    if size >= 1_000_000:
        h1.append(f"{size} bytes")
    bad_fonts = [f["name"] for f in fonts if not (f["emb"] and f["uni"])]
    if bad_fonts:
        h1.append("fonts not embedded/Unicode-mapped: " + ", ".join(bad_fonts))
    res.add("H1", FAIL if h1 else PASS, "PDF tagged, page budget, size, fonts",
            "; ".join(h1) or f"{pages} page(s), {size // 1024} KB, {len(fonts)} fonts all embedded + Unicode")
    type3 = [f["name"] for f in fonts if "Type 3" in f["type"]]
    if type3:
        res.add("H1", WARN, "No Type 3 fonts", ", ".join(type3))

    # H2 + H3 per extractor; H6 + H17 placement per extractor
    found_by_mode = {}
    for mode, text in texts.items():
        if text is None:
            res.add("H2", SKIP, f"Codepoints [{mode}]", f"{mode} not installed")
            continue
        bad = []
        lig = re.findall("[\ufb00-\ufb06]", text)
        if lig:
            bad.append(f"{len(lig)} ligature codepoint(s)")
        if "\u00ad" in text:
            bad.append(f"{text.count(chr(0xad))} soft hyphen(s)")
        spaced = re.findall(r"\b(?:\w ){3,}\w\b", text)
        if spaced:
            bad.append("letter-spaced: " + ", ".join(repr(s) for s in spaced[:3]))
        res.add("H2", FAIL if bad else PASS, f"No ligatures, soft hyphens, letter-spacing [{mode}]", "; ".join(bad))

        chars = [c for c in text if not c.isspace()]
        symbols = [c for c in chars if not (c.isalnum() or c in ".,;:'\"()-–—/&|%+#!?’“”‘@$*")]
        ws = re.findall(r"\S+", text)
        avg = sum(map(len, ws)) / max(1, len(ws))
        ls = [l for l in text.split("\n") if l.strip()]
        short = sum(1 for l in ls if len(l.split()) <= 2) / max(1, len(ls))
        longest = max((len(l) for l in ls), default=0)
        h3 = []
        if len(symbols) / max(1, len(chars)) >= 0.05:
            h3.append(f"symbols {len(symbols) / len(chars):.1%}")
        if not 4 <= avg <= 20:
            h3.append(f"average word length {avg:.1f}")
        if short > 0.5 or longest > 2000:
            h3.append(f"{short:.0%} of lines have ≤2 words; longest line {longest}")
        res.add("H3", FAIL if h3 else PASS, f"Textkernel garbage heuristics [{mode}]",
                "; ".join(h3) or f"symbols {len(symbols) / max(1, len(chars)):.2%}, avg word {avg:.1f}, short lines {short:.0%}")

        hits = locate_positions(text, positions)
        found_by_mode[mode] = hits
        missing = [f"{p['title']} | {p['company']} | {p['range']}" for p, h in zip(positions, hits) if not h]
        res.add("H6", FAIL if missing else PASS, f"Title, employer and dates adjacent, in order [{mode}]",
                (f"{len(missing)}/{len(positions)} not found together: " + "; ".join(missing[:4])) if missing
                else f"all {len(positions)} positions")

    # H4 header/footer leakage: a CLI print with Chrome's default header/footer on
    cli = cli_print(url, out / "print-with-headers.pdf")
    if not cli:
        res.add("H4", FAIL, "No Chrome header/footer in the PDF", "chromium --print-to-pdf wrote nothing")
    else:
        hf_text = run("pdftotext", "-enc", "UTF-8", str(cli), "-").stdout
        leaks = []
        if re.search(r"https?://|127\.0\.0\.1|localhost", hf_text):
            leaks.append("URL")
        if "clientid=" in hf_text:
            leaks.append("clientid=")
        hf_pages = int(pdf_info(cli).get("Pages", 0))
        if re.search(rf"(?<![\w/-])\d{{1,3}}/{hf_pages}(?![\w/-])", hf_text):
            leaks.append(f"N/{hf_pages} page number")
        if re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", hf_text):
            leaks.append("M/D/YY date")
        for t in {title_before, info.get("Title", "")} - {""}:
            if t in hf_text:
                leaks.append(f"title {t!r}")
        res.add("H4", FAIL if leaks else PASS, "No Chrome header/footer in the PDF (printed with headers on)", ", ".join(leaks))

    # H5 contact at top
    top = nonempty_lines(main_text)[:15]
    joined = " ".join(top)
    me = data.get("me", {})
    location = facts["contact"].split(" | ")[0] if facts["contact"] else ""
    wanted = {"name": me.get("name"), "email": me.get("email"), "location": location}
    site_host = next((urllib.parse.urlparse(c["url"]).netloc for c in data.get("companies", [])
                      if c.get("type") == "owned" and c.get("url")), None)
    if site_host:
        wanted["site"] = site_host
    absent = [k for k, v in wanted.items() if v and v not in joined]
    emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", joined))
    res.add("H5", FAIL if absent else (WARN if len(emails) != 1 else PASS), "Contact block in the first 15 lines",
            ("missing: " + ", ".join(absent)) if absent else f"{', '.join(wanted)}; {len(emails)} email(s)")
    if not any(me.get(k) for k in ("phone", "telephone")):
        res.add("H5", WARN, "Phone number present", "resume.json `me` has no phone (content; resume-json)")

    # H7 headings
    lines = nonempty_lines(main_text)
    want_h = [h for h in HEADINGS if h in facts["h2"]]
    exact = [h for h in want_h if h in lines]
    upper = [h for h in want_h if h not in lines and h.upper() in lines]
    gone = [h for h in want_h if h not in lines and h.upper() not in lines]
    odd_h2 = [h for h in facts["h2"] if h not in HEADINGS]
    h7 = []
    if gone:
        h7.append("heading not on a line of its own: " + ", ".join(gone))
    if odd_h2:
        h7.append("non-standard h2: " + ", ".join(odd_h2))
    if facts["h1"] != [me.get("name")]:
        h7.append(f"h1 is {facts['h1']}")
    res.add("H7", FAIL if h7 else PASS, "Standard section headings, h1 = name", "; ".join(h7) or ", ".join(want_h))
    if upper:
        res.add("H7", WARN, "Headings keep their case in the text layer",
                "CSS text-transform prints " + ", ".join(h.upper() for h in upper) + " instead of " + ", ".join(upper))
    outline_titles = [t for _, t in oline]
    o_bad = [h for h in want_h if h not in outline_titles]
    res.add("H7", WARN if o_bad else PASS, "PDF outline has the exact heading words",
            ("outline shows " + ", ".join(t for t in outline_titles if t.lower() in [h.lower() for h in o_bad])) if o_bad
            else f"{len(oline)} outline entries")

    # H8 tag text vs text layer
    flat = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    tag_flat, layer_flat = flat(stext["text"]), flat(texts["pdftotext-raw"])
    sm = difflib.SequenceMatcher(None, tag_flat, layer_flat, autojunk=False)
    ratio = sm.ratio()
    untagged = [layer_flat[b1:b2] for op, a1, a2, b1, b2 in sm.get_opcodes() if op in ("insert", "replace") and b2 - b1 > 2]
    res.add("H8", PASS if ratio > 0.99 else FAIL, "Tag tree text equals the text layer",
            f"character similarity {ratio:.3f}" + (f"; {len(untagged)} run(s) missing from the tags, e.g. "
                                                   + ", ".join(untagged[:6]) if untagged else ""))
    layer_words = set(words(texts["pdftotext-raw"]))
    merged = sorted({w for w in words(stext["text"]) if w not in layer_words and len(w) > 6})
    if merged:
        res.add("H8", WARN, "Tag tree keeps word spaces at line wraps",
                f"{len(merged)} merged word(s) in the tag text, e.g. " + ", ".join(merged[:5]))
    res.add("H8", PASS if ops["outside_marked"] == 0 else FAIL, "No text outside marked content",
            f"{ops['outside_marked']} of {ops['text_ops']} text-show ops outside BDC/EMC ({ops['streams']} streams)")
    if stext["errors"]:
        res.add("H8", WARN, "Struct tree uses only PDF 1.7 types (poppler)", "; ".join(stext["errors"]))

    # H9 struct tree roles
    h9 = []
    if ostruct["aside"]:
        h9.append(f"{ostruct['aside']} /Aside")
    if ostruct["figures"] != ostruct["figures_with_alt"]:
        h9.append(f"{ostruct['figures'] - ostruct['figures_with_alt']} Figure(s) without Alt")
    res.add("H9", FAIL if h9 else PASS, "No Aside; every Figure has Alt",
            "; ".join(h9) or f"{ostruct['figures']} figure(s); tags: {', '.join(ostruct['tags'])}")

    # H10 static vs rendered vs print
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw_html, "lxml")
    raw_app = soup.find(id="app")
    raw_text = norm(raw_app.get_text(" ")) if raw_app else ""
    real = ctx["datasets"]["default"]   # the static HTML always carries the real default view
    a_plus = [x for x in (real["me"].get("name"), real["me"].get("email"), (real.get("intros") or {}).get(sem["default_tier"]))
              if x and norm(x) not in raw_text]
    res.add("H10", FAIL if a_plus else PASS, "Raw HTML (no JS) has name, email, summary", "missing: " + ", ".join(a_plus) if a_plus else "")
    if name == "default":
        rendered_text = norm(BeautifulSoup(facts and (out / "dom.html").read_text(encoding="utf-8"), "lxml").find(id="app").get_text(" "))
        same = rendered_text == raw_text
        if not same:
            sm = difflib.SequenceMatcher(None, raw_text, rendered_text, autojunk=False)
            diff = [f"{op}: {raw_text[a1:a2][:60]!r} → {rendered_text[b1:b2][:60]!r}" for op, a1, a2, b1, b2 in sm.get_opcodes() if op != "equal"][:3]
        res.add("H10", PASS if same else FAIL, "Static HTML equals the default rendered view", "" if same else "; ".join(diff))
    dom_words = set(words(facts["appText"])) | set(words(" ".join(facts["h2"])))
    compounds = {}   # every run of 2+ adjacent parts, since a wrap can fall at any one hyphen
    for c in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", facts["appText"].lower()):
        parts = c.split("-")
        for i in range(len(parts)):
            for j in range(i + 2, len(parts) + 1):
                compounds.setdefault("".join(parts[i:j]), c)
    extra = sorted(set(words(main_text)) - dom_words)
    dehyphenated = [compounds[w] for w in extra if w in compounds]
    extra = [w for w in extra if w not in compounds]
    res.add("H10", PASS if not extra else FAIL, "PDF text ⊆ rendered page text", ("not on screen: " + ", ".join(extra[:10])) if extra else "")
    if dehyphenated:
        res.add("H2", WARN, "Hyphenated words survive extraction [pdftotext]",
                f"wrapped at the hyphen and joined without it: {', '.join(dehyphenated)}")

    # H11 hidden text
    in_app = [h for h in hidden if h["in"] == "app"]
    allowed = {"announce"} | {"controls"}
    elsewhere = [h for h in hidden if h["in"] != "app" and h["in"] not in allowed]
    res.add("H11", FAIL if in_app else PASS, "No hidden text in the resume (DOM)",
            "; ".join(f"{h['text'][:40]!r} ({', '.join(h['why'])})" for h in in_app[:4]))
    if elsewhere:
        res.add("H11", WARN, "No hidden text outside the resume", "; ".join(f"{h['in']}: {h['text'][:40]!r} ({', '.join(h['why'])})" for h in elsewhere[:4]))
    notes = [f"{h['in']}: {h['text'][:50]!r}" for h in hidden if h["in"] in allowed]
    pdf_hidden = [f"{ops[k]} {k.replace('_', ' ')}" for k in ("white_text", "invisible_text", "tiny_text") if ops[k]]
    res.add("H11", FAIL if pdf_hidden else PASS, "No white, invisible or tiny text in the PDF", ", ".join(pdf_hidden))
    if lint["hiddenInPrint"]:
        res.add("H11", WARN, "Nothing in the resume hidden only in print", ", ".join(lint["hiddenInPrint"][:5]))
    digest["hidden_allowed"] = notes

    # H12 head metadata (raw HTML)
    meta = lambda **a: (soup.find("meta", attrs=a) or {}).get("content")
    canon = (soup.find("link", rel="canonical") or {}).get("href")
    h12 = []
    for label, v in {"title": soup.title and soup.title.string, "description": meta(name="description"),
                     "og:title": meta(property="og:title"), "og:description": meta(property="og:description"),
                     "og:type": meta(property="og:type"), "og:url": meta(property="og:url"),
                     "og:image": meta(property="og:image"), "twitter:card": meta(name="twitter:card")}.items():
        if not v:
            h12.append(f"no {label}")
    if not canon or "?" in canon:
        h12.append(f"canonical {canon!r}")
    img = meta(property="og:image")
    if img:
        local = ROOT / urllib.parse.urlparse(img).path.lstrip("/")
        if not local.is_file():
            h12.append(f"og:image {img} not in repo")
        else:
            from PIL import Image
            with Image.open(local) as im:
                if im.width < 1200 or im.height < 627:
                    h12.append(f"og:image {im.width}×{im.height}")
    ld_tag = soup.find("script", type="application/ld+json")
    try:
        ld = json.loads(ld_tag.string) if ld_tag else None
    except json.JSONDecodeError as e:
        ld, _ = None, h12.append(f"JSON-LD invalid: {e}")
    if ld is None and ld_tag is None:
        h12.append("no static JSON-LD")
    elif ld:
        if "schema.org" not in str(ld.get("@context")) or ld.get("@type") != "Person" or not ld.get("name"):
            h12.append("JSON-LD is not a schema.org Person with a name")
        for banned in ("birthDate", "image"):
            if banned in ld:
                h12.append(f"JSON-LD has {banned}")
        facts_ld = [ld.get("name"), ld.get("email"), ld.get("jobTitle"), (ld.get("address") or {}).get("addressLocality")]
        invisible = [f for f in facts_ld if f and norm(f) not in raw_text]
        if invisible:
            h12.append("JSON-LD facts not visible: " + ", ".join(invisible))
    try:
        json.loads(facts["ld"] or "null")
    except json.JSONDecodeError:
        h12.append("JSON-LD invalid after JS")
    res.add("H12", FAIL if h12 else PASS, "Head metadata, Open Graph, JSON-LD", "; ".join(h12))

    # H13 variants (the cross-config byte comparison happens in main)
    want_noindex = bool(cfg["query"])
    has_noindex = "noindex" in (facts["robots"] or "")
    h13 = []
    if want_noindex != has_noindex:
        h13.append("noindex " + ("missing" if want_noindex else "present on the canonical page"))
    if facts["canonical"] != canon:
        h13.append(f"canonical changed to {facts['canonical']!r}")
    res.add("H13", FAIL if h13 else PASS, "noindex on variants only; canonical unchanged", "; ".join(h13))

    # H14 extractors on the DOM
    if readable:
        rt = norm(readable["text"])
        lost = [p[k] for p in positions for k in ("title", "company") if norm(p[k]) not in rt]
        res.add("H14", FAIL if lost else PASS, "Readability keeps every title and employer",
                ("lost: " + ", ".join(sorted(set(lost))[:6])) if lost else f"{readable['length']} chars kept")
    else:
        res.add("H14", FAIL, "Readability keeps every title and employer", "Readability returned nothing")
    try:
        import trafilatura
        tt = trafilatura.extract((out / "dom.html").read_text(encoding="utf-8"), include_formatting=False) or ""
        dates = [d for p in positions for d in (p["range"] or "").split(" – ") if d and d not in tt]
        h14 = ([f"{len(dates)} date(s) lost"] if dates else []) + ([] if "skills" in tt.lower() else ["'Skills' lost"])
        res.add("H14", FAIL if h14 else PASS, "Trafilatura keeps every date and 'Skills'", "; ".join(h14))
    except ImportError:
        res.add("H14", SKIP, "Trafilatura keeps every date and 'Skills'", "trafilatura not installed")

    # H15 axe-core
    for scheme, violations in axe.items():
        wcag = [v for v in violations if any(t.startswith("wcag") for t in v["tags"])]
        res.add("H15", FAIL if wcag else (WARN if violations else PASS), f"axe-core 4.10.2 [{scheme}]",
                "; ".join(f"{v['id']} ({v['impact']}, {v['nodes']} node(s))" for v in violations))

    # H16 print-media accessibility tree
    live = [n for n in ax_nodes if not n.get("ignored")]
    heads = []
    for n in live:
        if n.get("role", {}).get("value") == "heading":
            level = next((p["value"]["value"] for p in n.get("properties", []) if p["name"] == "level"), None)
            heads.append((level, n.get("name", {}).get("value", "")))
    h16, prev = [], 0
    if [l for l, _ in heads].count(1) != 1:
        h16.append(f"{[l for l, _ in heads].count(1)} level-1 headings")
    for level, label in heads:
        if level and level > prev + 1:
            h16.append(f"jump h{prev}→h{level} at {label!r}")
        prev = level or prev
    roles = [n.get("role", {}).get("value") for n in live]
    for bad in ("complementary", "figure", "img", "meter", "progressbar"):
        if bad in roles:
            h16.append(f"{roles.count(bad)} {bad} role(s)")
    tier_skills = [s["name"] for s in data.get("skills", []) if eff["tier"] in (s.get("tiers") or [])]
    skill_names = 0
    for li in facts["skills"]:
        rest = ", " + li.split(": ", 1)[-1] + ", "
        for n in sorted(tier_skills, key=len, reverse=True):   # names can contain ", "
            if f", {n}, " in rest:
                skill_names += 1
                rest = rest.replace(f", {n}, ", ", ", 1)
        skill_names += len([x for x in rest.split(", ") if x.strip()])   # unknown names still count
    avail = len(tier_skills)
    want_skills = min(sem["skill_count"].get(eff["skills"], 0), avail)
    if skill_names != want_skills:
        h16.append(f"{skill_names} skills shown, slider wants {want_skills}")
    want_recs = min(eff["recs"], len(data.get("recommendations", [])))
    if facts["recs"] != want_recs:
        h16.append(f"{facts['recs']} recommendations shown, slider wants {want_recs}")
    cap = sem["accomplishments"].get(eff["verbosity"], 0)
    over = [p["title"] for p in positions if p.get("wins", 0) > cap]
    if over:
        h16.append(f"{len(over)} position(s) over {cap} accomplishments")
    res.add("H16", FAIL if h16 else PASS, "Print accessibility tree: heading order, roles, slider counts",
            "; ".join(h16) or f"{len(heads)} headings; {skill_names} skills; {facts['recs']} recommendations")
    digest["ax_headings"] = heads

    # H17 human skim
    hits = found_by_mode.get("pdftotext", [])
    flat_lines = nonempty_lines(main_text)
    first = lambda s: next((i + 1 for i, l in enumerate(flat_lines) if s and norm(s) in l), None)
    anchors = {"name": first(me.get("name"))}
    for i, p in enumerate(positions[:3]):
        if p["kind"] == "firm":
            continue
        anchors.setdefault("current title", first(p["title"]))
        anchors.setdefault("current employer", first(p["company"]))
        anchors.setdefault("current dates", first(p["range"].split(" – ")[0]))
    if len(positions) > 1:
        prev_p = next((p for p in positions[1:] if p["kind"] != "firm" and p["title"] != positions[0]["title"]), None)
        if prev_p:
            anchors["previous title"] = first(prev_p["title"])
            anchors["previous employer"] = first(prev_p["company"])
    late = {k: v for k, v in anchors.items() if v is None or v > 15}
    res.add("H17", WARN if late else PASS, "Six skim anchors in the first ~15 PDF lines",
            ", ".join(f"{k} line {v}" for k, v in anchors.items()))
    if not data.get("education"):
        res.add("H17", WARN, "Education anchor present", "no education in resume.json (content; resume-json)")
    split = [f"{p['title']} ({p['range']})" for p, h in zip(positions, hits) if h and not h["same_page"]]
    res.add("H17", FAIL if split else PASS, "Each position's title and dates on the same page", "; ".join(split))
    gfx = []
    if facts["images"]:
        gfx.append(f"{facts['images']} image/svg/meter element(s) in the resume")
    if images:
        gfx.append(f"{images} image(s) in the PDF")
    res.add("H17", FAIL if gfx else PASS, "No rating graphics, photo or images", "; ".join(gfx))
    if cfg["query"].get("clientid"):
        prep = facts.get("prepared")
        res.add("H17", PASS if prep and eff["client"] and eff["client"] in prep and eff["client"] in main_text else FAIL,
                "\"Prepared for\" shown on screen and in the PDF", prep or "absent")
        res.add("H17", PASS if facts["tailoring"] == 0 else FAIL, "Locked page has no tailoring controls",
                f"{facts['tailoring']} tailoring input(s)")

    # H18 print CSS lint
    f = lint["found"]
    h18_fail = []
    multi_grid = [g for g in f["grid"] if "1 column track)" not in g]
    for k, v in (("multi-column grid", multi_grid), ("flex", f["flex"]), ("columns", f["columns"]), ("float", f["float"]),
                 ("letter-spacing > 0.1em", f["letterSpacing"]), ("ligatures on", f["ligatures"]), ("right-aligned dates", f["rightAligned"])):
        if v:
            h18_fail.append(f"{k}: {', '.join(v[:3])}")
    if lint["pageMargin"] not in ("0", "0px"):
        h18_fail.append(f"@page margin {lint['pageMargin']!r}")
    bad_breaks = [f"{k} {v}" for k, v in lint["breaks"].items() if v not in ("ok", "absent")]
    h18_fail += bad_breaks
    unreadable = [l for l in lint["links"] if not l["text"] or l["text"].lower() in ("here", "link", "click here")]
    if unreadable:
        h18_fail.append(f"{len(unreadable)} link(s) without readable text")
    res.add("H18", FAIL if h18_fail else PASS, "Print CSS: single column, no ligatures/letter-spacing, @page margin 0, breaks",
            "; ".join(h18_fail))
    single_grid = [g for g in f["grid"] if "1 column track)" in g]
    if single_grid:
        res.add("H18", WARN, "No grid layout in print (single track)", ", ".join(single_grid))
    if f["textTransform"]:
        res.add("H18", WARN, "No text-transform in print", ", ".join(f["textTransform"][:4]))

    # H19 file name / title
    display = eff["client"] if eff["locked"] else eff["tier"]
    want_title = f"{slugify(me.get('name'))}-Resume-{slugify(display)}-{date.today().isoformat()}"
    got = info.get("Title", "")
    res.add("H19", PASS if got == want_title else FAIL, "Print title = file name",
            f"got {got!r}" + ("" if got == want_title else f", want {want_title!r}"))
    res.add("H19", PASS if title_after == title_before else FAIL, "Title restored after printing",
            "" if title_after == title_before else f"{title_before!r} → {title_after!r}")

    # H21 phone
    res.add("H21", FAIL if phone_overflow else PASS, "No horizontal scroll at 390 px", "")

    write_digest(out, digest, texts)
    return digest


def timezone_check(browser, port, ctx):
    """H19b: the date in the file name is the reader's local date, not UTC."""
    zone = "Pacific/Kiritimati"   # UTC+14: its date differs from UTC's for 14 hours a day
    tab = browser.tab()
    try:
        tab.send("Emulation.setTimezoneOverride", {"timezoneId": zone})
        tab.goto(f"http://127.0.0.1:{port}/{PAGE}")
        title = tab.eval("dispatchEvent(new Event('beforeprint')); const t = document.title; dispatchEvent(new Event('afterprint')); t")
    finally:
        tab.close()
    local = datetime.now(ZoneInfo(zone)).date().isoformat()
    utc = datetime.now(ZoneInfo("UTC")).date().isoformat()
    if local == utc:
        return SKIP, f"local and UTC dates agree right now ({local}); rerun later to test"
    got = title[-10:]
    return (PASS if got == local else FAIL), f"in {zone} the title ends {got}; local date {local}, UTC {utc}"


def robots_check():
    robots = (ROOT / "robots.txt")
    if not robots.is_file():
        return PASS, "no robots.txt"
    maps = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", robots.read_text(encoding="utf-8"))
    if not maps:
        return PASS, "no Sitemap: line"
    problems, listed = [], []
    for m in maps:
        u = urllib.parse.urlparse(m)
        if u.netloc and u.netloc != urllib.parse.urlparse(SITE).netloc:
            problems.append(f"{m} is off-site")
            continue
        local = ROOT / u.path.lstrip("/")
        if not local.is_file():
            problems.append(f"{m} does not exist in the repo")
            continue
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", local.read_text(encoding="utf-8"))
        for loc in locs:
            p = urllib.parse.urlparse(loc).path.lstrip("/") or "index.html"
            (listed if (ROOT / p).is_file() else problems).append(loc if (ROOT / p).is_file() else f"{loc} missing")
    return (FAIL if problems else PASS), "; ".join(problems) or f"sitemap lists {len(listed)} existing page(s)"


# ---------------------------------------------------------------- reports

def write_digest(out, d, texts):
    res, info, facts, eff = d["res"], d["info"], d["facts"], d["effective"]
    L = [f"# Harness digest: `{d['name']}`", "",
         f"- URL: `{d['url']}`",
         f"- Effective: tier **{eff['tier']}**, " + ", ".join(f"{k} {eff[k]}" for k in eff if k not in ("tier", "locked", "client"))
         + (f", locked for **{eff['client']}**" if eff["locked"] else ""),
         f"- PDF: {info.get('Pages')} page(s), {info.get('File size')}, tagged {info.get('Tagged')}, title `{info.get('Title')}`",
         f"- Files in this folder: `resume.pdf`, `pdf-page1.png`, `desktop.png`, `phone.png`, `dom.html`, `raw.html`, `text-*.txt`",
         "", "## Checks", "", "| Check | Status | What | Detail |", "|---|---|---|---|"]
    for i in sorted(res.items, key=lambda i: int(i["id"][1:])):
        L.append(f"| {i['id']} | {i['status']} | {i['title']} | {md(i['detail'])} |")
    L += ["", "## Positions as rendered (DOM order) and where pdftotext finds them", "",
          "| # | Kind | Title | Employer | Dates | Line | Page |", "|---|---|---|---|---|---|---|"]
    hits = locate_positions(texts["pdftotext"], facts["positions"])
    for n, (p, h) in enumerate(zip(facts["positions"], hits), 1):
        L.append(f"| {n} | {p['kind']} | {md(p['title'])} | {md(p['company'])} | {p['range']} | "
                 f"{h['date_line'] + 1 if h else '—'} | {h['page'] if h else '—'} |")
    L += ["", "## Headings", "",
          "- Print accessibility tree: " + " → ".join(f"h{l} {t}" for l, t in d["ax_headings"]),
          "- PDF outline: " + " / ".join(f"{t} (p{p})" for p, t in d["outline"]),
          f"- Struct types: {', '.join(sorted(set(d['struct']['types'])))}",
          "", "## Head after JS", "", f"- title: {facts['title']}", f"- robots: {facts['robots']}",
          f"- canonical: {facts['canonical']}", f"- JSON-LD: `{facts['ld']}`", ""]
    axe_lines = [f"- [{s}] {v['id']} ({v['impact']}): {v['help']} — {v['nodes']} node(s), e.g. `{'`, `'.join(v['targets'])}`"
                 for s, vs in d["axe"].items() for v in vs]
    L += ["## axe-core violations", ""] + (axe_lines or ["None."]) + [""]
    if d["hidden_allowed"]:
        L += ["## Visually hidden by design (not resume content)", ""] + [f"- {h}" for h in d["hidden_allowed"]] + [""]
    if d["readable"]:
        L += ["## Readability", "", f"Title `{d['readable']['title']}`, {d['readable']['length']} characters. First 400:", "",
              "> " + norm(d["readable"]["text"])[:400], ""]
    L += ["## PDF text (`pdftotext -layout`)", "", "```text", texts["pdftotext-layout"].replace("\f", "\n----- page break -----\n").rstrip(), "```", ""]
    (out / "digest.md").write_text("\n".join(L), encoding="utf-8")
    (out / "checks.json").write_text(json.dumps(res.items, indent=2, ensure_ascii=False), encoding="utf-8")


def md(s):
    return str(s or "").replace("|", "\\|").replace("\n", " ")


def write_summary(digests, site_checks, started):
    ids = sorted({i["id"] for d in digests for i in d["res"].items}, key=lambda x: (len(x), x))
    L = [f"# Harness summary ({date.today().isoformat()})", "",
         f"Page `{PAGE}`, {len(digests)} configuration(s), {time.time() - started:.0f} s. "
         "Per-configuration detail: `harness-out/<config>/digest.md`.", "",
         "Status per check is the worst of its sub-checks. SKIP = tool not installed.", "",
         "| Config | Pages | " + " | ".join(ids) + " |", "|---|---|" + "---|" * len(ids)]
    for d in digests:
        pages = d.get("info", {}).get("Pages", "—") if d.get("facts") else "—"
        L.append(f"| {d['name']} | {pages} | " + " | ".join(d["res"].worst(i) for i in ids) + " |")
    L += ["", "## Site-wide", ""] + [f"- {cid} {st}: {title}. {detail}" for cid, st, title, detail in site_checks]
    L += ["", "## Every FAIL and WARN, grouped", ""]
    groups = {}
    for d in digests:
        for i in d["res"].items:
            if i["status"] in (FAIL, WARN):
                groups.setdefault((i["id"], i["status"], i["title"], i["detail"]), []).append(d["name"])
    for (cid, st, title, detail), names in sorted(groups.items(), key=lambda kv: ([FAIL, WARN].index(kv[0][1]), len(kv[0][0]), kv[0][0])):
        where = "all configs" if len(names) == len(digests) else ", ".join(names)
        L.append(f"- **{cid} {st}** {title}: {detail or '—'} _({where})_")
    (OUT / "summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- main

def vendor():
    CACHE.mkdir(exist_ok=True)
    out = {}
    for key, (url, sha) in VENDOR.items():
        path = CACHE / Path(urllib.parse.urlparse(url).path).name.replace(".min", f"-{url.split('/')[-2].split('@')[-1]}.min") \
            if key == "axe" else CACHE / f"readability-{url.split('@')[-1].split('/')[0]}.js"
        if not path.is_file():
            path.write_bytes(urllib.request.urlopen(url, timeout=30).read())
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != sha:
            sys.exit(f"{path} does not match its pinned hash; delete it and rerun.")
        out[key] = body.decode("utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="configuration names to run (see --list)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    source = (ROOT / PAGE).read_text(encoding="utf-8")
    sem = page_semantics(source)
    src = re.search(r"default:\s*'([^']+\.json)'", source)[1]
    anon = re.search(r"anon:\s*'([^']+\.json)'", source)[1]
    datasets = {"default": json.loads((ROOT / src).read_text(encoding="utf-8")),
                "anon": json.loads((ROOT / anon).read_text(encoding="utf-8"))}
    clients = json.loads((ROOT / "clients" / "clients.json").read_text(encoding="utf-8"))
    configs = configurations(datasets["default"], clients, sem)
    if args.list:
        print("\n".join(c["name"] for c in configs))
        return 0
    if args.only:
        unknown = set(args.only) - {c["name"] for c in configs}
        if unknown:
            sys.exit(f"unknown configuration(s): {', '.join(sorted(unknown))}; see --list")
        configs = [c for c in configs if c["name"] in args.only]

    ctx = {"sem": sem, "datasets": datasets, "clients": clients, "vendor": vendor()}
    OUT.mkdir(exist_ok=True)
    started = time.time()
    browser = Browser()          # before the server thread: the browser is forked with preexec_fn
    server = serve()
    port = server.server_address[1]
    digests = []
    try:
        for cfg in configs:
            t0 = time.time()
            d = run_config(browser, port, cfg, ctx)
            digests.append(d)
            counts = {s: sum(1 for i in d["res"].items if i["status"] == s) for s in (FAIL, WARN)}
            pages = d.get("info", {}).get("Pages", "?")
            print(f"{cfg['name']:<32} {pages} page(s)  {counts[FAIL]} FAIL  {counts[WARN]} WARN  ({time.time() - t0:.0f} s)")
        site = []
        shas = {d["raw_sha"] for d in digests}
        site.append(("H13", PASS if len(shas) == 1 else FAIL, "Raw HTML byte-identical across query strings",
                     f"{len(digests)} fetch(es), {len(shas)} distinct body(ies)"))
        st, detail = timezone_check(browser, port, ctx)
        site.append(("H19", st, "File-name date is the reader's local date", detail))
        st, detail = robots_check()
        site.append(("H20", st, "robots.txt Sitemap resolves", detail))
    finally:
        browser.close()
        server.shutdown()
    write_summary(digests, site, started)
    for cid, st, title, detail in site:
        print(f"{cid} {st}: {title}. {detail}")
    print(f"wrote {OUT.relative_to(ROOT)}/summary.md")
    failed = any(i["status"] == FAIL for d in digests for i in d["res"].items) or any(s[1] == FAIL for s in site)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
