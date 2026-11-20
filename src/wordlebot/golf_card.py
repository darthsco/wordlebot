"""The golf scorecard drawn as a PNG, styled like the admin panel's card.

Pure apart from reading the bundled fonts: given a round's ranking it returns
image bytes. The front nine sits above the back nine, so the card is narrow
enough to read on a phone; the back nine appears once the round reaches it.
Marks follow paper scorecards: circles under par (a gold disc for an ace),
squares over, a red X for a failed hole. Colours are the panel's dark theme,
which is also Discord's default.
"""

from __future__ import annotations

import io
import re
import unicodedata
from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .golf import FAILED, MISSED, NINE, PAR, YET, Milestone, Player, position, stage

FONTS = Path(__file__).parent / "fonts"
SCALE = 2  # pixels per layout unit, so text stays sharp when a phone zooms in

COLOURS = {
    "bg": "#1c1f22",
    "text": "#e6e8ea",
    "muted": "#9aa1a9",
    "border": "#33383d",
    "fairway": "#2c5a34",
    "fairway_dark": "#22472a",
    "fairway_line": "#3a6c43",
    "fairway_text": "#eef6ef",
    "par_bg": "#1f2a22",
    "sum_bg": "#262a2e",
    "under": "#ff7a70",
    "over": "#7fb0ec",
    "gold": "#e0b43a",
    "gold_text": "#2a1f00",
    "fail_bg": "#3d2220",
    "bad": "#ef6b62",
}

# Layout, in units (pixels before SCALE).
PAD, GAP, TITLE_H = 16, 18, 64
NAME_W, CELL_W, SUM_W, ROW_H = 150, 40, 48, 36
MARK_R = 13
LEGEND_ROW_H = 32

FILENAME = "golf-card.png"


@cache
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(str(FONTS / name), size * SCALE)


@cache
def _has_glyph(char: str) -> bool:
    """Whether the name font can draw `char`: a missing glyph renders as the font's
    empty box, the same as a private-use character the font certainly lacks."""
    return char.isspace() or _glyph(char) != _glyph("")


@cache
def _glyph(char: str) -> bytes:
    image = Image.new("L", (40, 40))
    ImageDraw.Draw(image).text((4, 4), char, fill=255, font=_font(14))
    return image.tobytes()


def _drawable(name: str) -> str:
    """`name` without what the font can't draw (emoji, most CJK) and the joiners
    and variation selectors around them; "?" if nothing is left."""
    # Emoji sequences ("🏌️‍♀️") go whole: their parts may be drawable on their own.
    name = re.sub(r"\S\ufe0f|\S?\u200d\S?", "", name)
    kept = "".join(
        c for c in name
        if unicodedata.category(c) not in ("Cf", "Mn", "Cc") and _has_glyph(c)
    )
    return " ".join(kept.split()) or "?"


class _Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.image = Image.new("RGB", (width * SCALE, height * SCALE), COLOURS["bg"])
        self.draw = ImageDraw.Draw(self.image)

    def box(self, x: float, y: float, w: float, h: float, fill: str | None = None,
            outline: str | None = COLOURS["border"], width: int = 1) -> None:
        self.draw.rectangle(
            [x * SCALE, y * SCALE, (x + w) * SCALE - 1, (y + h) * SCALE - 1],
            fill=fill, outline=outline, width=width * SCALE,
        )

    def text(self, x: float, y: float, s: str, colour: str, font: ImageFont.FreeTypeFont,
             anchor: str = "lm") -> None:
        self.draw.text((x * SCALE, y * SCALE), s, fill=colour, font=font, anchor=anchor)

    def centred(self, x: float, y: float, w: float, s: str, colour: str,
                font: ImageFont.FreeTypeFont) -> None:
        self.text(x + w / 2, y + ROW_H / 2, s, colour, font, "mm")

    def width_of(self, s: str, font: ImageFont.FreeTypeFont) -> float:
        return self.draw.textlength(s, font=font) / SCALE

    def fitted(self, s: str, font: ImageFont.FreeTypeFont, width: float) -> str:
        """`s`, cut short with an ellipsis if it is wider than `width`."""
        if self.width_of(s, font) <= width:
            return s
        while s and self.width_of(s + "…", font) > width:
            s = s[:-1]
        return s.rstrip() + "…"

    def hatch(self, x: float, y: float, w: float) -> None:
        """A hole still to come: diagonal stripes, as on the panel."""
        tile = Image.new("RGB", (int(w * SCALE), ROW_H * SCALE), COLOURS["bg"])
        draw = ImageDraw.Draw(tile)
        for k in range(-ROW_H, int(w), 7):
            draw.line([(k * SCALE, ROW_H * SCALE), ((k + ROW_H) * SCALE, 0)],
                      fill=COLOURS["par_bg"], width=2 * SCALE)
        self.image.paste(tile, (int(x * SCALE), int(y * SCALE)))
        self.box(x, y, w, ROW_H)

    def mark(self, cx: float, cy: float, cell: str) -> None:
        """A hole's score, marked as the panel marks it (see `golf.hole_mark`)."""
        def bounds(r: float) -> list[float]:
            return [(cx - r) * SCALE, (cy - r) * SCALE, (cx + r) * SCALE, (cy + r) * SCALE]

        line = int(1.5 * SCALE)
        font, colour = _font(15), COLOURS["text"]
        if cell in (MISSED, YET):
            colour = COLOURS["muted"]
        elif cell == FAILED:
            self.draw.rounded_rectangle(bounds(MARK_R), radius=4 * SCALE, fill=COLOURS["fail_bg"])
            font, colour = _font(15, True), COLOURS["bad"]
        else:
            diff = int(cell) - PAR
            if cell == "1":
                self.draw.ellipse(bounds(MARK_R + 3), outline=COLOURS["gold"], width=line)
                self.draw.ellipse(bounds(MARK_R), fill=COLOURS["gold"])
                font, colour = _font(15, True), COLOURS["gold_text"]
            elif diff <= -2:
                self.draw.ellipse(bounds(MARK_R), outline=COLOURS["under"], width=line)
                self.draw.ellipse(bounds(MARK_R - 4), outline=COLOURS["under"], width=line)
                font, colour = _font(13, True), COLOURS["under"]
            elif diff == -1:
                self.draw.ellipse(bounds(MARK_R), outline=COLOURS["under"], width=line)
                font, colour = _font(15, True), COLOURS["under"]
            elif diff == 1:
                self.draw.rectangle(bounds(MARK_R), outline=COLOURS["over"], width=line)
                colour = COLOURS["over"]
            elif diff >= 2:
                self.draw.rectangle(bounds(MARK_R), outline=COLOURS["over"], width=line)
                self.draw.rectangle(bounds(MARK_R - 4), outline=COLOURS["over"], width=line)
                font, colour = _font(13), COLOURS["over"]
        self.text(cx, cy, cell, colour, font, "mm")


def _to_par(value: int) -> str:
    return "E" if value == 0 else f"{value:+d}".replace("-", "−")


def render(
    milestone: Milestone,
    ranked: list[tuple[int, Player]],
    fail_score: int,
    max_players: int = 0,
    live: bool = False,
) -> bytes:
    """The round's scorecard as a PNG: the top `max_players` (0 = everyone) of
    `ranked`, front nine above back nine. `live` as for `golf.card`."""
    m = milestone
    shown = ranked[:max_players] if max_players > 0 else ranked
    starts = [0, NINE] if m.holes > NINE else [0]
    # The last section carries the round's totals; a lone front nine's Out is its total.
    extras = [["Out"], ["In", "Tot", "±"]] if len(starts) == 2 else [["Out", "±"]]
    widths = [NAME_W + NINE * CELL_W + SUM_W * len(e) for e in extras]
    section_h = ROW_H * (2 + len(shown))
    legend = [
        [("1", "ace"), ("2", "eagle"), ("3", "birdie"), ("4", "par"), ("5", "bogey"),
         ("6", "double bogey")],
        [(FAILED, "failed"), (MISSED, f"not played (scores {fail_score})")]
        + ([(YET, "yet to play today")] if live else []),
    ]
    width = PAD * 2 + max(widths)
    height = (PAD * 2 + TITLE_H + len(starts) * (section_h + GAP)
              + len(legend) * LEGEND_ROW_H)
    c = _Canvas(width, height)

    last = m.first + m.holes - 1
    span = f"Wordle {m.first:,}" if m.holes == 1 else f"Wordle {m.first:,}–{last:,}"
    c.text(PAD, PAD + 16, f"Golf round {m.round_no} · {stage(m, live)}", COLOURS["text"],
           _font(22, True))
    c.text(PAD, PAD + 46, f"{span} · par {PAR * m.holes}", COLOURS["muted"], _font(14))

    y = PAD + TITLE_H
    for start, extra, section_w in zip(starts, extras, widths):
        top = y
        holes = range(start, start + NINE)
        # Hole row.
        c.box(PAD, y, NAME_W, ROW_H, fill=COLOURS["fairway"], outline=COLOURS["fairway_line"])
        c.text(PAD + 10, y + ROW_H / 2, "Hole", COLOURS["fairway_text"], _font(14, True))
        x = PAD + NAME_W
        for h in holes:
            c.box(x, y, CELL_W, ROW_H, fill=COLOURS["fairway"], outline=COLOURS["fairway_line"])
            c.centred(x, y, CELL_W, str(h + 1), COLOURS["fairway_text"], _font(14, True))
            x += CELL_W
        for label in extra:
            c.box(x, y, SUM_W, ROW_H, fill=COLOURS["fairway_dark"], outline=COLOURS["fairway_line"])
            c.centred(x, y, SUM_W, label, COLOURS["fairway_text"], _font(14, True))
            x += SUM_W
        y += ROW_H
        # Par row.
        c.box(PAD, y, NAME_W, ROW_H, fill=COLOURS["par_bg"])
        c.text(PAD + 10, y + ROW_H / 2, "Par", COLOURS["muted"], _font(13))
        x = PAD + NAME_W
        for _ in holes:
            c.box(x, y, CELL_W, ROW_H, fill=COLOURS["par_bg"])
            c.centred(x, y, CELL_W, str(PAR), COLOURS["muted"], _font(13))
            x += CELL_W
        par_of = {"Out": PAR * NINE, "In": PAR * NINE, "Tot": PAR * 2 * NINE}
        for label in extra:
            c.box(x, y, SUM_W, ROW_H, fill=COLOURS["sum_bg"])
            c.centred(x, y, SUM_W, str(par_of.get(label, "")), COLOURS["muted"], _font(13))
            x += SUM_W
        y += ROW_H
        # Players.
        for rank, p in shown:
            c.box(PAD, y, NAME_W, ROW_H)
            c.text(PAD + 10, y + ROW_H / 2, position(rank, ranked), COLOURS["muted"], _font(12))
            name = c.fitted(_drawable(p.name), _font(14), NAME_W - 50)
            c.text(PAD + 42, y + ROW_H / 2, name, COLOURS["text"], _font(14))
            x = PAD + NAME_W
            for h in holes:
                if h < m.holes:
                    c.box(x, y, CELL_W, ROW_H)
                    c.mark(x + CELL_W / 2, y + ROW_H / 2, p.cells[h])
                else:
                    c.hatch(x, y, CELL_W)
                x += CELL_W
            for label in extra:
                c.box(x, y, SUM_W, ROW_H, fill=COLOURS["sum_bg"])
                if label == "±":
                    colour = (COLOURS["under"] if p.to_par < 0
                              else COLOURS["over"] if p.to_par > 0 else COLOURS["text"])
                    c.centred(x, y, SUM_W, _to_par(p.to_par), colour, _font(14, True))
                else:
                    value = {"Out": p.strokes_in(0, NINE), "In": p.strokes_in(NINE, 2 * NINE),
                             "Tot": p.total}[label]
                    c.centred(x, y, SUM_W, str(value), COLOURS["text"], _font(14, True))
                x += SUM_W
            y += ROW_H
        c.box(PAD - 2, top - 2, section_w + 4, section_h + 4, outline=COLOURS["fairway"], width=2)
        y += GAP

    y += 4
    for row in legend:
        x = PAD + 14
        for cell, label in row:
            c.mark(x, y + 12, cell)
            c.text(x + 20, y + 12, label, COLOURS["muted"], _font(13))
            x += 20 + c.width_of(label, _font(13)) + 26
        y += LEGEND_ROW_H

    out = io.BytesIO()
    c.image.save(out, format="PNG", optimize=True)
    return out.getvalue()
