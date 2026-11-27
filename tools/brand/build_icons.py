#!/usr/bin/env python3
"""Regenerate the Crewboss logo + web icon set into app/static/brand/crewboss/ (task #705).

Run from the repo root in a throwaway container (nothing is installed on the host):

  docker run --rm -v "$PWD":/w -w /w python:3.12-slim sh -c "apt-get update -qq && apt-get install -y -qq libcairo2 \
    && pip install -q cairosvg fonttools pillow uharfbuzz && python tools/brand/build_icons.py \
    && chown -R $(id -u):$(id -g) app/static/brand/crewboss tools/brand"

(cairosvg needs the libcairo2 system library, hence apt in the container; the chown hands the output back.)
Sources of truth: the geometry + colours below, and tools/brand/fonts/Sora-VF.ttf (OFL, see OFL-Sora.txt), which
is instanced at wght=600 and outlined so no output file depends on the font.
"""
import io
import math
import struct
from pathlib import Path

import cairosvg
import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parent.parent.parent
OUT = ROOT / "app/static/brand/crewboss"
FONT = ROOT / "tools/brand/fonts/Sora-VF.ttf"

LIGHT = dict(accent="#c05a33", ink="#17171a", page="#f6f5f0")
DARK = dict(accent="#de7c56", ink="#f4f3ee", page="#131315")
# Raster fallbacks (PNG favicons + ICO) cannot follow the colour scheme, so the crew dots are a mid warm grey that holds
# >= 3:1 on both page colours (3.17:1 on #f6f5f0, 5.37:1 on #131315); the lead keeps the copper (4.05 / 4.20).
FALLBACK = dict(accent=LIGHT["accent"], ink="#8c8a83", page=LIGHT["page"])
TILE_DOT = "#fbfaf7"
TILE_RADIUS = 0.22

# Full mark, 64 grid. Concept A refined: arms are straight lines from the lead (same dx per row), the edge gap
# lead->row1 equals row1->row2 (8 units), and the whole mark is centred vertically (7.4 / 7.5 margin).
FULL = [("a", 32, 15.4, 8), ("i", 20.5, 34.1, 6), ("i", 43.5, 34.1, 6), ("i", 9, 50.5, 6), ("i", 55, 50.5, 6)]
# Small mark (16-24 px): concept geometry; the lead y 17 -> 16 so that at 16 px every edge lands on a pixel
# boundary (lead (8,4) r3, wings (3.5,11.5) r2.5 -> spans 1..7, 1..6/11, 9..14) and at 32 px likewise.
SMALL = [("a", 32, 16, 12), ("i", 14, 46, 10), ("i", 50, 46, 10)]


def circles(dots, c, tile=False, tf=""):
    out = []
    for kind, x, y, r in dots:
        fill = TILE_DOT if tile else c["accent" if kind == "a" else "ink"]
        out.append(f'<circle cx="{x:g}" cy="{y:g}" r="{r:g}" fill="{fill}"/>')
    return "".join(out)


def svg(w, h, body, vb=None, extra=""):
    vb = vb or f"0 0 {w:g} {h:g}"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{w:g}" height="{h:g}"{extra}>'
            f"{body}</svg>\n")


def mark(dots, c):
    return svg(64, 64, circles(dots, c))


def fit_scale(dots, radius):
    """Scale about (32,32) so every dot sits inside `radius` (64-grid units scaled)."""
    far = max(math.hypot(x - 32, y - 32) + r for _, x, y, r in dots)
    return radius / far


def tile(size, rounded, dots=FULL, radius_frac=0.40 * 0.97, accent=LIGHT["accent"]):
    s = fit_scale(dots, radius_frac * size)
    rx = TILE_RADIUS * size if rounded else 0
    body = (f'<rect width="{size}" height="{size}" rx="{rx:g}" fill="{accent}"/>'
            f'<g transform="translate({size/2:g} {size/2:g}) scale({s:.5f}) translate(-32 -32)">'
            f"{circles(dots, LIGHT, tile=True)}</g>")
    return svg(size, size, body)


# ---- wordmark: Sora SemiBold outlined -------------------------------------------------------------------
def outline_word(text="crewboss", split=4, tracking=-0.02, size=100.0):
    vf = TTFont(FONT)
    inst = instantiateVariableFont(vf, {"wght": 600}, inplace=False)
    buf = io.BytesIO()
    inst.save(buf)
    face = hb.Face(buf.getvalue())
    font = hb.Font(face)
    b = hb.Buffer()
    b.add_str(text)
    b.guess_segment_properties()
    hb.shape(font, b, {"kern": True, "liga": False})
    upem = inst["head"].unitsPerEm
    k = size / upem
    gs = inst.getGlyphSet()
    order = inst.getGlyphOrder()
    x = 0.0
    parts = ["", ""]
    for i, (info, pos) in enumerate(zip(b.glyph_infos, b.glyph_positions)):
        pen = SVGPathPen(gs, ntos=lambda v: f"{v:.2f}".rstrip("0").rstrip("."))
        gs[order[info.codepoint]].draw(TransformPen(pen, (k, 0, 0, -k, x + pos.x_offset * k, 0)))
        parts[0 if i < split else 1] += pen.getCommands()
        x += pos.x_advance * k + tracking * size
    x -= tracking * size
    asc = inst["OS/2"].sCapHeight * k  # not used for layout; the real extents come from the cairo bbox below
    return parts, x, size


def word_bbox(parts):
    """Ink bbox of the outlined path, via cairosvg's own parser (so it matches what is drawn)."""
    from cairosvg.parser import Tree  # noqa: F401  (import check only)
    # cheap exact route: render big and measure
    s = svg(1400, 400, f'<g transform="translate(100 300)"><path d="{parts[0]}{parts[1]}"/></g>')
    from PIL import Image
    im = Image.open(io.BytesIO(cairosvg.svg2png(bytestring=s.encode()))).convert("RGBA")
    l, t, r, bt = im.getchannel("A").getbbox()
    return l - 100, t - 300, r - 100, bt - 300


def wordmark_svgs():
    parts, adv, size = outline_word()
    l, t, r, bt = word_bbox(parts)
    pad = 4
    w, h = r - l + 2 * pad, bt - t + 2 * pad
    res = {}
    for name, c in (("", LIGHT), ("-dark", DARK)):
        g = (f'<g transform="translate({pad - l:.2f} {pad - t:.2f})">'
             f'<path fill="{c["ink"]}" d="{parts[0]}"/><path fill="{c["accent"]}" d="{parts[1]}"/></g>')
        res[name] = (svg(round(w, 2), round(h, 2), g, f"0 0 {w:.2f} {h:.2f}"), (l, t, r, bt), parts)
    return res


def lockup_svg(c, parts, bbox):
    l, t, r, bt = bbox
    # Mark at full 64 height; word scaled so its x-height band centres on the mark's vertical centre (32).
    # The word's ascender top / baseline are l..: place baseline so (t+bt)/2 -> 32 after scaling by k.
    k = 0.42  # word height ~ 0.42*(bt-t)... tuned by eye: ascender height ~ 0.62 of the mark
    wh = (bt - t) * k
    ty = 32 - ((t + bt) / 2) * k
    gap = 12
    tx = 64 + gap - l * k
    wtotal = 64 + gap + (r - l) * k
    body = (circles(FULL, c) + f'<g transform="translate({tx:.2f} {ty:.2f}) scale({k})">'
            f'<path fill="{c["ink"]}" d="{parts[0]}"/><path fill="{c["accent"]}" d="{parts[1]}"/></g>')
    return svg(round(wtotal, 2), 64, body, f"0 0 {wtotal:.2f} 64")


def favicon_svg():
    css = (".a{fill:%s}.i{fill:%s}@media (prefers-color-scheme:dark){.a{fill:%s}.i{fill:%s}}"
           % (LIGHT["accent"], LIGHT["ink"], DARK["accent"], DARK["ink"]))
    body = f"<style>{css}</style>"
    for kind, x, y, r in SMALL:
        body += f'<circle class="{kind}" cx="{x:g}" cy="{y:g}" r="{r:g}"/>'
    return svg(64, 64, body)


def png(svg_text, px):
    return cairosvg.svg2png(bytestring=svg_text.encode(), output_width=px, output_height=px)


def ico(frames):
    """Hand-built ICO with PNG-compressed frames (size -> png bytes), so each size is exactly what we rendered."""
    n = len(frames)
    head = struct.pack("<HHH", 0, 1, n)
    off = 6 + 16 * n
    ents, blobs = b"", b""
    for size, data in frames:
        ents += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), off + len(blobs))
        blobs += data
    return head + ents + blobs


def write(name, data):
    p = OUT / name
    p.write_bytes(data if isinstance(data, bytes) else data.encode())


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for sfx, c in (("", LIGHT), ("-dark", DARK)):
        write(f"mark{sfx}.svg", mark(FULL, c))
        write(f"mark-small{sfx}.svg", mark(SMALL, c))
    words = wordmark_svgs()
    for sfx, c in (("", LIGHT), ("-dark", DARK)):
        s, bbox, parts = words[sfx]
        write(f"wordmark{sfx}.svg", s)
        write(f"lockup{sfx}.svg", lockup_svg(c, parts, bbox))
    write("mark-tile.svg", tile(512, True))
    write("favicon.svg", favicon_svg())

    plain = {16: SMALL, 32: SMALL, 48: FULL, 64: FULL, 96: FULL}
    for px, dots in plain.items():
        write(f"favicon-{px}.png", png(mark(dots, FALLBACK), px))
    write("badge-96.png", png(mark(SMALL, LIGHT), 96))               # push badge: only the alpha channel is used
    write("favicon-180.png", png(tile(180, False), 180))             # apple-touch: square, opaque (iOS masks it)
    write("favicon-192.png", png(tile(192, True), 192))
    write("favicon-512.png", png(tile(512, True), 512))
    write("maskable-192.png", png(tile(192, False), 192))             # full-bleed; mark within the 80% safe circle
    write("maskable-512.png", png(tile(512, False), 512))
    write("favicon.ico", ico([(16, png(mark(SMALL, FALLBACK), 16)), (32, png(mark(SMALL, FALLBACK), 32)),
                              (48, png(mark(FULL, FALLBACK), 48))]))
    print("ok", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
