#!/usr/bin/env python3
"""render.py — compiles every image in this profile's README.

Each letter is shaped with HarfBuzz and outlined to an SVG path at build time.
No web fonts, no JavaScript, no requests at view time: the same pixels on every
machine. Output is deterministic, and the colophon prints its own hash.

    pip install fonttools==4.66.1 uharfbuzz==0.56.3
    python3 render.py
"""

import hashlib
import html
import io
import pathlib
import urllib.request

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
    top = header(doc, "Victor Hugo", "@zerotohero3", "(00)", left_cls="ink")

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


# (01) — the line that stays.
def quote(theme):
    doc = Doc("quote", "“Stay hungry, stay foolish.” — Steve Jobs", theme)
    top = header(doc, "(01)", "First principle")

    s1, s2 = 116, 156
    caret_w, caret_gap = 6, 10
    b1 = top + 44 + 0.74 * s1
    b2 = b1 + 0.98 * s2
    line1 = doc.text(bold, "“Stay hungry,", -bold.ink("“", s1)[0], b1, s1, track=-0.035)
    x2 = W - caret_w - caret_gap - serif.ink("stay foolish.”", s2, -0.01)[1]
    line2 = doc.text(serif, "stay foolish.”", x2, b2, s2, track=-0.01)

    lift = 1.25 * s2
    c1 = doc.clip("l1", -10, b1 - 0.98 * s1, W + 20, 1.26 * s1)
    c2 = doc.clip("l2", -10, b2 - 0.92 * s2, W + 20, 1.18 * s2)
    doc.add(
        f'<g clip-path="url(#{c1})"><g class="rv">{line1}</g></g>',
        f'<g clip-path="url(#{c2})"><g class="rv" style="animation-delay:.14s">{line2}</g></g>',
        f'<rect class="acc caret" x="{W - caret_w}" y="{b2 - 0.66 * s2:.1f}" width="{caret_w}" height="{0.74 * s2:.1f}"/>',
    )
    doc.css.append(
        f"@keyframes rv{{from{{transform:translateY({lift:.0f}px)}}to{{transform:none}}}}"
        f".rv{{animation:rv 1.4s {REVEAL} .1s both}}"
        "@keyframes blink{50%{opacity:0}}.caret{animation:blink 1.1s steps(1) infinite}"
    )

    by = b2 + 78
    doc.add(
        label(doc, "— Steve Jobs", 0, by, "ink"),
        label(doc, "Stanford — 12 Jun 2005", W, by - 20, anchor="end"),
        label(doc, "After the Whole Earth Catalog, 1974", W, by, anchor="end"),
    )
    doc.h = by + 22
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


LINKS = [
    ("linkedin", "LinkedIn", "/in/vhsoftdev", "https://linkedin.com/in/vhsoftdev/"),
    ("instagram", "Instagram", "@trvpsxvnt", "https://www.instagram.com/trvpsxvnt"),
]


# (03) — elsewhere. One image per row so each row is its own link.
def elsewhere(theme):
    doc = Doc("elsewhere", "Elsewhere", theme)
    doc.h = header(doc, "(03)", "Elsewhere") + 1
    return doc


def link(theme, n, key, name, handle):
    doc = Doc(key, f"{name} — {handle}", theme)
    h, size = 96, 44
    b = h / 2 + 0.36 * size
    doc.add(
        label(doc, f"{n:02d}", 0, b),
        doc.text(sans, name, 48, b, size, track=-0.03),
        label(doc, handle, 560, b),
    )
    a = 30  # the arrow exits up-right and re-enters from below-left, like a hover that hovers itself
    ax, ay = W - 6, b
    box = doc.clip("arrow", W - 60, b - 46, 60, 60)
    arrow = doc.text(sans, "↗", ax, ay, 40, "acc", "end")
    doc.add(f'<g clip-path="url(#{box})"><g class="ar" style="animation-delay:{1.2 + 1.6 * n:.1f}s">{arrow}</g></g>')
    doc.css.append(
        f"@keyframes ar{{0%,80%{{transform:none;animation-timing-function:cubic-bezier(.7,0,.84,0)}}"
        f"88%{{transform:translate({a * 2}px,-{a * 2}px);animation-timing-function:step-end}}"
        f"89%{{transform:translate(-{a * 2}px,{a * 2}px);animation-timing-function:{REVEAL}}}to{{transform:none}}}}"
        f".ar{{animation:ar 5s infinite}}"
    )
    doc.rule(h - 1)
    doc.h = h
    return doc


def colophon(theme, rev):
    doc = Doc("colophon", f"Colophon — rev {rev}", theme)
    y, lh, size = 34, 19, 11
    left = ["Set in Geist, Geist Mono & Instrument Serif", "Shaped by HarfBuzz, outlined to SVG paths", "0 web fonts · 0 JavaScript · 0 trackers"]
    right = [f"Rev {rev}", "© 2026 Victor Hugo", "EOF"]
    for i, (l, r) in enumerate(zip(left, right)):
        doc.add(label(doc, l, 0, y + i * lh, size=size), label(doc, r, W, y + i * lh, anchor="end", size=size))
    doc.h = y + 2 * lh + 16
    return doc


def main():
    OUT.mkdir(exist_ok=True)
    files = {}
    for theme in THEMES:
        docs = [hero(theme), quote(theme), index(theme), elsewhere(theme)]
        docs += [link(theme, n, key, name, handle) for n, (key, name, handle, _) in enumerate(LINKS)]
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
