#!/usr/bin/env python3
"""render.py — compiles every image in this profile's README.

Each letter is shaped with HarfBuzz and outlined to an SVG path at build time.
No web fonts, no JavaScript, no requests at view time: the same pixels on every
machine. Output is deterministic, and the colophon prints its own hash.

    pip install fonttools==4.66.1 uharfbuzz==0.56.3
    python3 render.py             # compile assets/
    python3 render.py --preview   # ...and open a local replica of the GitHub page
"""

import functools
import hashlib
import html
import io
import json
import math
import os
import pathlib
import re
import sys
import urllib.request
import webbrowser

import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "assets"
CACHE = ROOT / ".cache"
W = 1000

# Tuned to GitHub's own palette so the page reads as one surface, not as cards.
THEMES = {
    "dark": dict(ink="#E6EDF3", mute="#7D8590", line="#30363D", acc="#FF5B1F"),
    "light": dict(ink="#1F2328", mute="#656D76", line="#D0D7DE", acc="#E8460C"),
}
EASE = "cubic-bezier(.76,0,.24,1)"
REVEAL = "cubic-bezier(.16,1,.3,1)"
PERIOD = "7s"


# Fonts are pinned to one google/fonts commit and checked byte for byte, so the
# same source always compiles to the same SVGs (and the same rev).
FONTS_AT = "https://raw.githubusercontent.com/google/fonts/bd8f81ddb5c74d5c8897b36ad88b440266245103/ofl/"
FONTS = {
    "geist/Geist%5Bwght%5D.ttf": "73894e0448cae90a92b6c2f8732b7bb9acb7b94c418bff559dad4a18e1de9659",
    "geistmono/GeistMono%5Bwght%5D.ttf": "d00e590b8eb3a59acc329b2d044fd143ae935090b7da33199ebee27cc7de8196",
    "instrumentserif/InstrumentSerif-Italic.ttf": "08939b8bdf534afec24ae0ef5e03f948940cd9a8fe08e7fecbad040e62327385",
}


def fetch(path):
    dst = CACHE / pathlib.Path(path).name.replace("%5B", "[").replace("%5D", "]")
    if not dst.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(FONTS_AT + path, dst)
    data = dst.read_bytes()
    if hashlib.sha256(data).hexdigest() != FONTS[path]:
        dst.unlink()
        raise SystemExit(f"checksum mismatch: {path}")
    return io.BytesIO(data)


class Face:
    def __init__(self, key, path, wght=None, features=None):
        tt = TTFont(fetch(path))
        if wght:
            tt = instancer.instantiateVariableFont(tt, {"wght": wght})
        buf = io.BytesIO()
        tt.save(buf)
        data = buf.getvalue()
        self.key, self.features = key, features or {}
        self.tt = TTFont(io.BytesIO(data))
        self.hb = hb.Font(hb.Face(data))
        self.glyphs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.upm = self.tt["head"].unitsPerEm

    def layout(self, text, track=0.0):
        """Shape `text`; returns [(gid, x)] and the advance, in font units."""
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb, buf, self.features)
        pen, out = 0.0, []
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            out.append((info.codepoint, pen + pos.x_offset))
            pen += pos.x_advance + track * self.upm
        return out, (pen - track * self.upm) if out else 0.0

    def width(self, text, size, track=0.0):
        return self.layout(text, track)[1] * size / self.upm

    def path(self, gid):
        pen = SVGPathPen(self.glyphs, ntos=lambda n: f"{n:.0f}")
        self.glyphs[self.order[gid]].draw(pen)
        return pen.getCommands()

    def bounds(self, gid):
        pen = BoundsPen(self.glyphs)
        self.glyphs[self.order[gid]].draw(pen)
        return pen.bounds

    def gid(self, char):
        return self.layout(char)[0][0][0]

    def ink(self, text, size, track=0.0):
        """Left and right edges of the inked area, in px from the pen origin."""
        glyphs, _ = self.layout(text, track)
        k = size / self.upm
        (g0, x0), (g1, x1) = glyphs[0], glyphs[-1]
        return (x0 + self.bounds(g0)[0]) * k, (x1 + self.bounds(g1)[2]) * k

    def rise(self, text, size):
        """Highest and lowest inked points, in px above (+) and below (-) the baseline."""
        boxes = [b for gid, _ in self.layout(text)[0] if (b := self.bounds(gid))]
        k = size / self.upm
        return max(b[3] for b in boxes) * k, min(b[1] for b in boxes) * k


class Doc:
    def __init__(self, name, title, theme):
        self.name, self.title, self.t = name, title, THEMES[theme]
        self.h, self.defs, self.body, self.css = 0, {}, [], []

    def glyph(self, face, gid):
        ref = f"{face.key}{gid}"
        if ref not in self.defs:
            self.defs[ref] = face.path(gid)
        return ref if self.defs[ref] else None

    def place(self, face, glyphs, x, y, size, cls="ink"):
        """Outlined glyphs at (x, y) baseline; glyph x positions in font units."""
        k = size / face.upm
        uses = "".join(
            f'<use href="#{ref}" x="{gx:.0f}"/>'
            for gid, gx in glyphs
            if (ref := self.glyph(face, gid))
        )
        return f'<g class="{cls}" transform="translate({x:.2f} {y:.2f}) scale({k:.5f} {-k:.5f})">{uses}</g>'

    def text(self, face, s, x, y, size, cls="ink", anchor="start", track=0.0):
        glyphs, adv = face.layout(s, track)
        w = adv * size / face.upm
        x -= {"start": 0, "middle": w / 2, "end": w}[anchor]
        return self.place(face, glyphs, x, y, size, cls)

    def add(self, *markup):
        self.body.extend(markup)

    def rule(self, y, x0=0, x1=W):
        self.add(f'<path class="rule" d="M{x0} {y}H{x1}"/>')

    def clip(self, key, x, y, w, h):
        ref = f"{self.name}-{key}"
        self.add(f'<clipPath id="{ref}"><rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"/></clipPath>')
        return ref

    def svg(self):
        t, title = self.t, html.escape(self.title)
        css = (
            f".ink{{fill:{t['ink']}}}.mute{{fill:{t['mute']}}}.acc{{fill:{t['acc']}}}"
            f".rule{{fill:none;stroke:{t['line']};stroke-width:1;vector-effect:non-scaling-stroke}}"
            + "".join(self.css)
            + "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
        )
        defs = "".join(f'<path id="{k}" d="{d}"/>' for k, d in self.defs.items() if d)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {self.h:.0f}" '
            f'width="{W}" height="{self.h:.0f}" role="img" aria-label="{title}">'
            f"<title>{title}</title><style>{css}</style><defs>{defs}</defs>"
            f'{"".join(self.body)}</svg>\n'
        )


SANS = "geist/Geist%5Bwght%5D.ttf"
sans = Face("a", SANS, 400)
bold = Face("b", SANS, 500)
mono = Face("m", "geistmono/GeistMono%5Bwght%5D.ttf", 400, {"calt": False, "liga": False})
serif = Face("s", "instrumentserif/InstrumentSerif-Italic.ttf")

LABEL = dict(size=12, track=0.12)
NAME = "Victor Hugo Macedo Paes Leme"


def label(doc, s, x, y, cls="mute", anchor="start", size=LABEL["size"]):
    return doc.text(mono, s.upper(), x, y, size, cls, anchor, LABEL["track"])


def header(doc, left, right, center=None, y=18, left_cls="mute"):
    doc.add(label(doc, left, 0, y, left_cls), label(doc, right, W, y, anchor="end"))
    if center:
        doc.add(label(doc, center, W / 2, y, anchor="middle"))
    doc.rule(y + 16)
    return y + 16


def swap_keyframes(out, inn, d):
    """`out` leaves upward while `inn` rises into place, then back again: a loop."""
    return (
        f"@keyframes {out}{{0%,36%{{transform:translateY(0);animation-timing-function:{EASE}}}"
        f"50%{{transform:translateY(-{d:.1f}px);animation-timing-function:step-end}}"
        f"86%{{transform:translateY({d:.1f}px);animation-timing-function:{EASE}}}"
        f"98%,to{{transform:translateY(0)}}}}"
        f"@keyframes {inn}{{0%,36%{{transform:translateY({d:.1f}px);animation-timing-function:{EASE}}}"
        f"50%,86%{{transform:translateY(0);animation-timing-function:{EASE}}}"
        f"98%{{transform:translateY(-{d:.1f}px);animation-timing-function:step-end}}"
        f"to{{transform:translateY({d:.1f}px)}}}}"
        f".{out}{{animation:{out} {PERIOD} infinite both}}.{inn}{{animation:{inn} {PERIOD} infinite both}}"
    )


# (00) — zero → hero. 'z' is 0111 1010, 'h' is 0110 1000: two bits apart.
def hero(theme):
    doc = Doc("hero", "zero → hero. 'z' (0111 1010) becomes 'h' (0110 1000): zero and hero are two bits apart.", theme)
    top = header(doc, NAME, "Software Engineer — São Paulo", "(00)", left_cls="ink")

    track = -0.035
    zero, _ = bold.layout("zero", track)
    word, _ = bold.layout("hero", track)
    gh, gz, go = bold.gid("h"), bold.gid("z"), bold.gid("o")
    h_box, o_box = bold.bounds(gh), bold.bounds(go)
    span = word[-1][1] + o_box[2] - h_box[0]
    size = W * bold.upm / span
    k = size / bold.upm
    x0 = -h_box[0] * k
    base = top + 56 + h_box[3] * k

    band_top, band_bot = base - h_box[3] * k - 6, base + 6
    dist = band_bot - band_top
    band = doc.clip("word", 0, band_top, W, dist)
    z_x = word[1][1] - zero[1][1]  # z keeps its own advance and kerning against 'e'

    doc.add(
        doc.place(bold, word[1:], x0, base, size),
        f'<g clip-path="url(#{band})">'
        f'<g class="z" transform="translate(0 {-dist:.1f})">{doc.place(bold, [(gz, z_x)], x0, base, size)}</g>'
        f'<g class="h">{doc.place(bold, [(gh, 0)], x0, base, size, "acc")}</g></g>',
    )
    doc.css.append(swap_keyframes("z", "h", dist))

    # Readout: only the cells that differ roll, left to right, in the accent.
    vsize, vb = 20, base + 104
    lb = vb - 34
    cell = mono.width("0", vsize)
    cols = [("char", "'z'", "'h'"), ("binary", "0111 1010", "0110 1000"), ("hex", "0x7A", "0x68")]
    gap = 44
    widths = [max(len(a) * cell, mono.width(n.upper(), LABEL["size"], LABEL["track"])) for n, a, _ in cols]
    x = W - sum(widths) - gap * (len(cols) - 1)
    rd = doc.clip("readout", x - 4, vb - vsize, W - x + 8, vsize * 1.35)
    rdist = vsize * 1.35
    rolls, n = [], 0
    for (name, a, b), w in zip(cols, widths):
        doc.add(label(doc, name, x, lb))
        for i, (ca, cb) in enumerate(zip(a, b)):
            cx = x + i * cell
            if ca == cb:
                doc.add(doc.text(mono, ca, cx, vb, vsize))
                continue
            delay = f' style="animation-delay:{0.12 + 0.05 * n:.2f}s"'
            rolls.append(
                f'<g class="zs" transform="translate(0 {-rdist:.1f})"{delay}>{doc.text(mono, ca, cx, vb, vsize)}</g>'
                f'<g class="hs"{delay}>{doc.text(mono, cb, cx, vb, vsize, "acc")}</g>'
            )
            n += 1
        x += w + gap
    doc.add(f'<g clip-path="url(#{rd})">{"".join(rolls)}</g>')
    doc.css.append(swap_keyframes("zs", "hs", rdist))

    doc.add(
        doc.text(serif, "Zero and hero are", 0, vb - 36, 36),
        doc.text(serif, "two bits apart.", 0, vb, 36),
    )
    doc.h = vb + 34
    return doc


# (01) — precision requires rupture. One cut at 0.618 of the x-height; the upper
# half slides right until it lands exactly on the margin the lower half misses.
def principle(theme):
    doc = Doc("principle", "precision requires rupture.", theme)
    top = header(doc, "(01)", "First principle")

    l1, l2 = "precision requires", "rupture."
    t1, t2 = -0.035, -0.01
    left, right = bold.ink(l1, 1, t1)
    s1 = W / (right - left)
    up1, down1 = bold.rise(l1, s1)
    b1 = top + 48 + up1
    s2 = 1.9 * s1
    up2, down2 = serif.rise(l2, s2)
    b2 = b1 - down1 + 40 + up2
    shift = round(0.065 * s2)
    x2 = W - shift - serif.ink(l2, s2, t2)[1]
    word = doc.text(serif, l2, x2, b2, s2, track=t2)
    cut = b2 - 0.618 * serif.tt["OS/2"].sxHeight * s2 / serif.upm

    lift = 1.3 * s2
    c1 = doc.clip("l1", -10, b1 - up1 - 8, W + 20, up1 - down1 + 16)
    c2 = doc.clip("l2", -10, b2 - up2 - 8, W + 20, up2 - down2 + 16)
    above = doc.clip("above", -10, b2 - up2 - 10, W + 40, cut - (b2 - up2 - 10))
    below = doc.clip("below", -10, cut, W + 40, b2 - down2 + 10 - cut)
    doc.add(
        f'<g clip-path="url(#{c1})"><g class="rv">{doc.text(bold, l1, -left * s1, b1, s1, track=t1)}</g></g>',
        f'<g clip-path="url(#{c2})"><g class="rv" style="animation-delay:.14s">'
        f'<g clip-path="url(#{below})">{word}</g>'
        f'<g clip-path="url(#{above})"><g class="rup" transform="translate({shift} 0)">{word}</g></g></g></g>',
        f'<rect class="acc cut" x="0" y="{cut - 0.5:.1f}" width="{W}" height="1"/>',
        f'<g class="tag">{doc.text(mono, "0.618", 0, cut - 10, 11, "acc", track=0.12)}</g>',
    )
    doc.css.append(
        f"@keyframes rv{{from{{transform:translateY({lift:.0f}px)}}to{{transform:none}}}}"
        f".rv{{animation:rv 1.4s {REVEAL} .1s both}}"
        f"@keyframes cut{{from{{transform:scaleX(0)}}}}"
        f".cut{{transform-box:fill-box;transform-origin:0 50%;animation:cut .8s {EASE} 1.2s both}}"
        f"@keyframes rup{{from{{transform:none}}to{{transform:translateX({shift}px)}}}}"
        f".rup{{animation:rup .7s cubic-bezier(.85,0,.15,1) 1.9s both}}"
        f"@keyframes tag{{from{{opacity:0}}}}.tag{{animation:tag .6s ease 1.9s both}}"
    )
    doc.h = b2 - down2 + 24
    return doc


STACK = [
    ("Backend", ["C#", "ASP.NET Core", "Entity Framework", "SQL Server"]),
    ("Patterns", ["Event-Driven", "Transactional Outbox"]),
    ("Interface", ["TypeScript", "React", "Tailwind CSS"]),
    ("Platform", ["Azure DevOps", "Docker", "Grafana"]),
    ("AI", ["MCP", "RAG", "SDD"]),
]


# (02) — the stack as a spec sheet. Zero-indexed, hex-numbered.
def index(theme):
    total = sum(len(t) for _, t in STACK)
    names = ", ".join(t for _, tools in STACK for t in tools)
    doc = Doc("index", f"Index of {total} tools: {names}.", theme)
    top = header(doc, "(02)", f"Index — {total:02d}")

    row, size, sup, item_gap, x_items = 72, 28, 11, 26, 230
    i, y = 0, top
    for r, (group, tools) in enumerate(STACK):
        b = y + row / 2 + 0.36 * size
        parts = [label(doc, f"{r:02d}", 0, b), label(doc, group, 48, b)]
        x = x_items
        for tool in tools:
            parts.append(doc.text(sans, tool, x, b, size, track=-0.02))
            x += sans.width(tool, size, -0.02) + 3
            parts.append(doc.text(mono, f"{i:X}", x, b - 0.42 * size, sup, "mute"))
            x += mono.width("0", sup) + item_gap
            i += 1
        parts.append(label(doc, f"({len(tools):02d})", W, b, anchor="end"))
        doc.add(f'<g class="up" style="animation-delay:{0.08 + 0.09 * r:.2f}s">{"".join(parts)}</g>')
        y += row
        doc.rule(y)
    doc.css.append(f"@keyframes up{{from{{opacity:0;transform:translateY(18px)}}}}.up{{animation:up 1.1s {REVEAL} both}}")
    doc.h = y + 1
    return doc


# Contributions to other people's code, newest first. `backticks` set a span in
# mono. `stars` is only the offline fallback: builds read the live count.
OSS = [
    dict(
        key="oss-csharp-sdk-1917",
        repo="modelcontextprotocol/csharp-sdk",
        stars=4596,
        number=1917,
        url="https://github.com/modelcontextprotocol/csharp-sdk/pull/1917",
        title="Fix dropped `notifications/cancelled` when a request is canceled",
        note="Remote handlers now actually get canceled when the caller cancels.",
        merged="22 Oct 2026",
        diff=(94, 4),
    ),
]


@functools.cache
def stars(repo, fallback):
    """Live star count, so the number on the page never goes stale; offline builds keep the last known one."""
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}", headers={"Accept": "application/vnd.github+json"})
    if token := os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return json.load(res)["stargazers_count"]
    except (OSError, ValueError, KeyError):
        return fallback


def star(cx, cy, r, cls="ink"):
    """GitHub's star, computed: no font here has one."""
    pts = " ".join(
        f"{cx + (r if i % 2 == 0 else r * 0.42) * math.cos(math.pi * (i / 5 - 0.5)):.2f},"
        f"{cy + (r if i % 2 == 0 else r * 0.42) * math.sin(math.pi * (i / 5 - 0.5)):.2f}"
        for i in range(10)
    )
    return f'<polygon class="{cls}" points="{pts}"/>'


# (03) — open source. One compact row per contribution, so the list can grow
# like the index above it instead of stacking cards. One image per row, so each
# row is its own link.
def oss_header(theme):
    doc = Doc("oss", f"Open source — {len(OSS)} merged contribution(s)", theme)
    doc.h = header(doc, "(03)", f"Open source — {len(OSS):02d}") + 1
    return doc


def contribution(theme, n, c):
    count = stars(c["repo"], c["stars"])
    shown = f"{count / 1000:.1f}k" if count >= 1000 else str(count)
    title = c["title"].replace("`", "")
    doc = Doc(c["key"], f"{c['repo']} ({shown} stars) #{c['number']} — {title}. Merged {c['merged']}.", theme)

    y = 32
    x = 48 + mono.width(c["repo"], 13, 0.04) + 24
    doc.add(
        label(doc, f"{n:02d}", 0, y),
        doc.text(mono, c["repo"], 48, y, 13, "ink", track=0.04),
        star(x + 5, y - 4.5, 6),
        label(doc, shown, x + 16, y, "ink"),
        label(doc, f"#{c['number']}", x + 16 + mono.width(shown.upper(), LABEL["size"], LABEL["track"]) + 20, y),
        label(doc, f"+{c['diff'][0]} −{c['diff'][1]}   ·   Merged {c['merged']}", W, y, anchor="end"),
    )

    # The title fits on one line beside the arrow; code spans are set in mono.
    runs = [(mono if i % 2 else sans, run) for i, run in enumerate(c["title"].split("`")) if run]
    size = 30
    fit = lambda fs: sum(f.width(r, fs * (0.9 if f is mono else 1)) for f, r in runs)
    while fit(size) > W - 48 - 80:
        size -= 1
    y += 46
    x = 48
    for face, run in runs:
        fs = size * (0.9 if face is mono else 1)
        doc.add(doc.text(face, run, x, y, fs))
        x += face.width(run, fs)
    arrow(doc, y, n)

    y += 34
    doc.add(doc.text(serif, c["note"], 48, y, 22, "mute"))
    doc.h = y + 24
    doc.rule(doc.h - 1)
    return doc


def arrow(doc, b, n):
    """The arrow exits up-right and re-enters from below-left, like a hover that hovers itself."""
    a = 60
    box = doc.clip("arrow", W - 60, b - 46, 60, 60)
    glyph = doc.text(sans, "↗", W - 6, b, 40, "acc", "end")
    doc.add(f'<g clip-path="url(#{box})"><g class="ar" style="animation-delay:{1.2 + 1.6 * n:.1f}s">{glyph}</g></g>')
    doc.css.append(
        f"@keyframes ar{{0%,80%{{transform:none;animation-timing-function:cubic-bezier(.7,0,.84,0)}}"
        f"88%{{transform:translate({a}px,-{a}px);animation-timing-function:step-end}}"
        f"89%{{transform:translate(-{a}px,{a}px);animation-timing-function:{REVEAL}}}to{{transform:none}}}}"
        f".ar{{animation:ar 5s infinite}}"
    )


def colophon(theme, rev):
    doc = Doc("colophon", f"Colophon — rev {rev}", theme)
    y, lh, size = 34, 19, 11
    left = ["Set in Geist, Geist Mono & Instrument Serif", "Shaped by HarfBuzz, outlined to SVG paths", "0 web fonts · 0 JavaScript · 0 trackers"]
    right = [f"Rev {rev}", f"© 2026 {NAME}", "EOF"]
    for i, (l, r) in enumerate(zip(left, right)):
        doc.add(label(doc, l, 0, y + i * lh, size=size), label(doc, r, W, y + i * lh, anchor="end", size=size))
    doc.h = y + 2 * lh + 16
    return doc


RAW = "https://raw.githubusercontent.com/zerotohero3/zerotohero3/main/assets/"

# Mirrors GitHub's README box: same widths, colors and markdown block rules. The
# images load as <img>, exactly as on GitHub, so the animations run for real.
PREVIEW = """<!doctype html><meta charset="utf-8"><title>README preview</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fff;--fg:#1f2328;--line:#d0d7de;--chip:#f6f8fa}
[data-theme=dark]{--bg:#0d1117;--fg:#e6edf3;--line:#30363d;--chip:#151b23}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"Segoe UI",sans-serif}
nav{position:sticky;top:0;display:flex;gap:6px;justify-content:center;padding:12px;background:var(--bg);border-bottom:1px solid var(--line);z-index:1}
button{font:inherit;color:inherit;background:var(--chip);border:1px solid var(--line);border-radius:6px;padding:4px 12px;cursor:pointer}
button[aria-pressed=true]{border-color:var(--fg)}
.box{max-width:var(--w,830px);margin:24px auto;border:1px solid var(--line);border-radius:6px;padding:32px;font-size:16px}
.box p{margin:0 0 16px}.box img{max-width:100%;box-sizing:content-box}
</style>
<nav><button data-theme="dark">Dark</button><button data-theme="light">Light</button>
<button data-w="830px">Desktop</button><button data-w="343px">Mobile</button><button id="replay">Replay</button></nav>
<div class="box">{body}</div>
<script>
const imgs = [...document.querySelectorAll("picture")].map(p => {
  const img = p.querySelector("img"), src = p.querySelector("source");
  img.dataset.light = img.getAttribute("src"); img.dataset.dark = src.srcset; src.remove();
  return img;
});
const press = (attr, v) => document.querySelectorAll(`[data-${attr}]`).forEach(b => b.setAttribute("aria-pressed", b.dataset[attr] === v));
function theme(t) { document.documentElement.dataset.theme = t; imgs.forEach(i => i.src = i.dataset[t]); press("theme", t); }
function width(w) { document.documentElement.style.setProperty("--w", w); press("w", w); }
document.querySelectorAll("[data-theme]").forEach(b => b.onclick = () => theme(b.dataset.theme));
document.querySelectorAll("[data-w]").forEach(b => b.onclick = () => width(b.dataset.w));
document.getElementById("replay").onclick = () => imgs.forEach(i => i.src = i.src.split("?")[0] + "?" + Date.now());
theme(matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); width("830px");
</script>
"""


def preview():
    md = re.sub(r"<!--.*?-->", "", (ROOT / "README.md").read_text(), flags=re.S)
    md = md.replace(RAW, OUT.as_uri() + "/")
    # cmark emits a block that opens with a lone tag line as raw HTML and wraps the rest in <p>.
    blocks = [b.strip() for b in re.split(r"\n\s*\n", md) if b.strip()]
    body = "\n".join(b if b.split("\n")[0] == "<picture>" else f"<p>{b}</p>" for b in blocks)
    page = CACHE / "preview.html"
    page.write_text(PREVIEW.replace("{body}", body))
    webbrowser.open(page.as_uri())
    print(f"preview → {page}")


def main():
    OUT.mkdir(exist_ok=True)
    files = {}
    for theme in THEMES:
        docs = [hero(theme), principle(theme), index(theme), oss_header(theme)]
        docs += [contribution(theme, n, c) for n, c in enumerate(OSS)]
        for doc in docs:
            files[f"{doc.name}.{theme}.svg"] = doc.svg()
    rev = hashlib.sha1("".join(files[k] for k in sorted(files)).encode()).hexdigest()[:7]
    for theme in THEMES:
        files[f"colophon.{theme}.svg"] = colophon(theme, rev).svg()
    for name, svg in files.items():
        (OUT / name).write_text(svg)
    print(f"rev {rev} · {len(files)} files · {sum(map(len, files.values())) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
    if "--preview" in sys.argv:
        preview()
