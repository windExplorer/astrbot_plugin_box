"""MoeMoe profile card renderer.

Draws the profile card in the same pastel style as the plugin logo:
pink gradient header with the avatar and nickname, a white body with
soft colored rows for each field, and a branded footer strip.
"""

from io import BytesIO
from pathlib import Path

import emoji
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .profile import BoxUserProfile

RESOURCE_DIR = Path(__file__).resolve().parent / "resource"

# ---------------------------------------------------------------- palette
HEADER_TOP = (255, 220, 236)
HEADER_BOTTOM = (255, 156, 200)
CARD_BG = (255, 255, 255)
ROW_STYLES = [  # (row background, label color), cycled like the logo bars
    ((255, 241, 247), (214, 84, 146)),
    ((247, 242, 255), (138, 100, 210)),
    ((239, 247, 255), (66, 142, 208)),
    ((255, 247, 232), (206, 138, 62)),
]
TEXT_DARK = (74, 59, 71)
TITLE_COLOR = (92, 62, 84)
SUBTITLE_COLOR = (255, 255, 255)
FOOTER_BG = (255, 232, 243)
FOOTER_TEXT = (191, 108, 152)
BORDER = (255, 194, 222)
SHADOW_COLOR = (190, 90, 140)

# ---------------------------------------------------------------- layout
CARD_W = 1040
PAD = 56
HEADER_H = 300
FOOTER_H = 78
AVATAR_D = 216
AVATAR_RING = 10
RADIUS = 48
ROW_PAD_X = 26
ROW_PAD_V = 9
ROW_GAP = 14
LINE_GAP = 6
FONT_SIZE = 34
TITLE_SIZE = 58
SUBTITLE_SIZE = 30
FOOTER_SIZE = 26
BODY_TOP_PAD = 26
BODY_BOTTOM_PAD = 26


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _rounded_mask(size: tuple, radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius, fill=255)
    return mask


def _sparkle(draw: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill: tuple) -> None:
    k = s * 0.22
    draw.polygon(
        [(cx, cy - s), (cx + k, cy - k), (cx + s, cy), (cx + k, cy + k),
         (cx, cy + s), (cx - k, cy + k), (cx - s, cy), (cx - k, cy - k)],
        fill=fill,
    )


def _heart(draw: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill: tuple) -> None:
    r = s * 0.42
    draw.ellipse([cx - s, cy - s * 0.55, cx, cy + r - s * 0.55], fill=fill)
    draw.ellipse([cx, cy - s * 0.55, cx + s, cy + r - s * 0.55], fill=fill)
    draw.polygon([(cx - s, cy - s * 0.15), (cx + s, cy - s * 0.15), (cx, cy + s)], fill=fill)


def _cat_face(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float) -> None:
    """Cute cat face, parameterized around the logo avatar proportions (r=95)."""
    k = r / 95
    ear = (255, 217, 160)
    inner = (255, 170, 185)
    face = (255, 224, 170)
    eye = (110, 72, 60)
    blush = (255, 148, 170, 150)
    whisk = (150, 110, 90, 110)

    draw.polygon(
        [(cx - 78 * k, cy - 62 * k), (cx - 96 * k, cy - 158 * k), (cx + 10 * k, cy - 96 * k)],
        fill=ear,
    )
    draw.polygon(
        [(cx + 78 * k, cy - 62 * k), (cx + 96 * k, cy - 158 * k), (cx - 10 * k, cy - 96 * k)],
        fill=ear,
    )
    draw.polygon(
        [(cx - 68 * k, cy - 70 * k), (cx - 78 * k, cy - 128 * k), (cx - 18 * k, cy - 94 * k)],
        fill=inner,
    )
    draw.polygon(
        [(cx + 68 * k, cy - 70 * k), (cx + 78 * k, cy - 128 * k), (cx + 18 * k, cy - 94 * k)],
        fill=inner,
    )
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=face)
    w = max(4, int(9 * k))
    draw.arc([cx - 62 * k, cy - 28 * k, cx - 18 * k, cy + 16 * k], 195, 345, fill=eye, width=w)
    draw.arc([cx + 18 * k, cy - 28 * k, cx + 62 * k, cy + 16 * k], 195, 345, fill=eye, width=w)
    mw = max(3, int(6 * k))
    draw.arc([cx - 17 * k, cy + 16 * k, cx + 1 * k, cy + 36 * k], 0, 180, fill=eye, width=mw)
    draw.arc([cx + 1 * k, cy + 16 * k, cx + 19 * k, cy + 36 * k], 0, 180, fill=eye, width=mw)
    bw = 34 * k
    bh = 22 * k
    draw.ellipse([cx - 84 * k, cy + 6 * k, cx - 84 * k + bw, cy + 6 * k + bh], fill=blush)
    draw.ellipse([cx + 84 * k - bw, cy + 6 * k, cx + 84 * k, cy + 6 * k + bh], fill=blush)
    ww = max(2, int(4 * k))
    draw.line([cx - 108 * k, cy - 12 * k, cx - 142 * k, cy - 22 * k], fill=whisk, width=ww)
    draw.line([cx - 108 * k, cy + 6 * k, cx - 142 * k, cy + 14 * k], fill=whisk, width=ww)
    draw.line([cx + 108 * k, cy - 12 * k, cx + 142 * k, cy - 22 * k], fill=whisk, width=ww)
    draw.line([cx + 108 * k, cy + 6 * k, cx + 142 * k, cy + 14 * k], fill=whisk, width=ww)


class CardMaker:
    """Renders QQ profile data into a moe-style profile card PNG."""

    def __init__(self, font_size: int = FONT_SIZE):
        self.font_size = font_size
        self.font = ImageFont.truetype(RESOURCE_DIR / "box.ttf", font_size)
        try:
            self.emoji_font = ImageFont.truetype(RESOURCE_DIR / "NotoColorEmoji.ttf", font_size)
        except OSError:
            self.emoji_font = ImageFont.truetype(RESOURCE_DIR / "NotoColorEmoji.ttf", 109)
        self._emoji_h = int(font_size * 1.18)
        self._font_cache: dict[int, ImageFont.FreeTypeFont] = {font_size: self.font}
        self._emoji_tiles: dict[str, Image.Image | None] = {}
        # the bundled emoji font is subsetted; missing glyphs would render as
        # tofu boxes, so keep its cmap to detect and skip unsupported emoji
        try:
            from fontTools.ttLib import TTFont

            self._cmap: set[int] | None = set(
                TTFont(str(RESOURCE_DIR / "NotoColorEmoji.ttf")).getBestCmap().keys()
            )
        except Exception:
            self._cmap = None

    # ------------------------------------------------------------ fonts
    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        font = self._font_cache.get(size)
        if font is None:
            font = ImageFont.truetype(RESOURCE_DIR / "box.ttf", size)
            self._font_cache[size] = font
        return font

    # ------------------------------------------------------------ text runs
    _SKIP_CHARS = frozenset("\u200d\ufe0f\u20e3")  # ZWJ / VS16 / keycap cap

    @staticmethod
    def _runs(text: str) -> list[tuple[str, bool]]:
        runs: list[tuple[str, bool]] = []
        for ch in text:
            if ch in CardMaker._SKIP_CHARS:
                continue
            is_em = emoji.is_emoji(ch)
            if runs and runs[-1][1] == is_em:
                runs[-1] = (runs[-1][0] + ch, is_em)
            else:
                runs.append((ch, is_em))
        return runs

    @staticmethod
    def _emoji_target(font: ImageFont.FreeTypeFont) -> int:
        return int(font.size * 1.15)

    def _measure(self, text: str, font: ImageFont.FreeTypeFont) -> float:
        width = 0.0
        emoji_h = self._emoji_target(font)
        for run, is_em in self._runs(text):
            width += emoji_h * len(run) if is_em else font.getlength(run)
        return width

    def _wrap(self, text: str, font: ImageFont.FreeTypeFont, limit: float) -> list[str]:
        emoji_h = self._emoji_target(font)
        lines: list[str] = []
        cur = ""
        cur_w = 0.0
        for run, is_em in self._runs(text):
            for ch in run:
                w = emoji_h if is_em else font.getlength(ch)
                if cur and cur_w + w > limit:
                    lines.append(cur)
                    cur = ""
                    cur_w = 0.0
                cur += ch
                cur_w += w
        if cur or not lines:
            lines.append(cur)
        return lines

    def _emoji_tile(self, ch: str) -> Image.Image | None:
        """Render an emoji glyph as a white-on-transparent tile (alpha = coverage).

        The bundled emoji font is a monochrome outline font, so glyphs are
        stored as masks and tinted with the surrounding text color when drawn.
        """
        if ch in self._emoji_tiles:
            return self._emoji_tiles[ch]
        if self._cmap is not None and ord(ch) not in self._cmap:
            self._emoji_tiles[ch] = None
            return None
        native = self.emoji_font.size
        canvas = native * 2 + 16
        img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        pad = native // 2
        ImageDraw.Draw(img).text((pad, pad), ch, font=self.emoji_font, fill=(255, 255, 255, 255))
        bbox = img.getbbox()
        tile = img.crop(bbox) if bbox else None
        self._emoji_tiles[ch] = tile
        return tile

    def _draw_mixed(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        pos: tuple[float, float],
        text: str,
        font: ImageFont.FreeTypeFont,
        fill: tuple,
        emoji_fill: tuple | None = None,
    ) -> None:
        x, y = pos
        efill = emoji_fill or fill
        ascent, _descent = font.getmetrics()
        emoji_h = self._emoji_target(font)
        for run, is_em in self._runs(text):
            if is_em:
                for ch in run:
                    tile = self._emoji_tile(ch)
                    if tile is not None:
                        w = max(1, int(tile.width * emoji_h / tile.height))
                        mask = tile.getchannel("A").resize((w, emoji_h), Image.LANCZOS)
                        tinted = Image.new("RGBA", mask.size, efill)
                        tinted.putalpha(mask)
                        ey = int(y + (ascent - emoji_h) / 2 + font.size * 0.06)
                        img.alpha_composite(tinted, (int(x), max(0, ey)))
                        x += w + 2
                    else:
                        x += font.size * 0.2
            else:
                draw.text((x, y), run, font=font, fill=fill)
                x += font.getlength(run)

    # ------------------------------------------------------------ rows
    @staticmethod
    def _parse_rows(lines: list[str]) -> list[list]:
        """Split display lines into [label, [value lines]] rows.

        Lines whose prefix before the full-width colon is a known field label
        start a row; everything else (library indents, wrapped signatures) is a
        continuation line of the previous row.
        """
        labels = set(BoxUserProfile.FIELD_LABELS.values())
        rows: list[list] = []
        for raw in lines:
            text = raw.strip()
            head, sep, tail = text.partition("：")
            if sep and head in labels:
                rows.append([head, [tail]])
            elif rows:
                rows[-1][1].append(text)
            else:
                rows.append(["", [text]])
        return rows

    # ------------------------------------------------------------ card
    def create(self, avatar: bytes, reply: list[str]) -> bytes:
        """Create a profile card PNG.

        Args:
            avatar: Avatar image bytes.
            reply: Display lines to render.

        Returns:
            Rendered PNG bytes.
        """
        rows = self._parse_rows(reply)
        title, subtitle, rows = self._split_header(rows)

        body_h = BODY_TOP_PAD + BODY_BOTTOM_PAD
        if rows:
            label_w = self._label_width(rows)
            body_h += sum(self._row_height(label, value, label_w) for label, value in rows)
            body_h += ROW_GAP * (len(rows) - 1)

        card_h = HEADER_H + body_h + FOOTER_H
        shadow_pad = 52
        canvas_w = CARD_W + shadow_pad * 2
        canvas_h = card_h + shadow_pad * 2

        img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

        # soft drop shadow
        shadow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            [shadow_pad, shadow_pad + 12, shadow_pad + CARD_W, shadow_pad + card_h + 12],
            RADIUS,
            fill=(*SHADOW_COLOR, 110),
        )
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(20)))

        # card base + header + footer
        ox, oy = shadow_pad, shadow_pad
        card = Image.new("RGBA", (CARD_W, card_h), (0, 0, 0, 0))
        cdraw = ImageDraw.Draw(card)
        cdraw.rounded_rectangle([0, 0, CARD_W - 1, card_h - 1], RADIUS, fill=(*CARD_BG, 255))
        self._paint_header(card)
        self._paint_footer(card, card_h)
        card.putalpha(_rounded_mask((CARD_W, card_h), RADIUS))
        img.alpha_composite(card, (ox, oy))

        draw = ImageDraw.Draw(img)

        # header content
        avatar_img = self._load_avatar(avatar)
        self._paint_header_content(img, draw, ox, oy, avatar_img, title, subtitle)

        # body rows
        row_x = ox + PAD
        row_w = CARD_W - PAD * 2
        label_w = self._label_width(rows) if rows else 0.0
        y = oy + HEADER_H + BODY_TOP_PAD
        for i, (label, values) in enumerate(rows):
            bg, label_color = ROW_STYLES[i % len(ROW_STYLES)]
            row_h = self._row_height(label, values, label_w)
            draw.rounded_rectangle([row_x, y, row_x + row_w, y + row_h], 20, fill=(*bg, 255))
            text_y = y + ROW_PAD_V
            value_x = row_x + ROW_PAD_X + label_w
            if label:
                draw.text((row_x + ROW_PAD_X, text_y), label, font=self.font, fill=(*label_color, 255))
            limit = row_x + row_w - ROW_PAD_X - value_x
            line_pitch = self._line_h() + LINE_GAP
            j = 0
            for value in values:
                for part in self._wrap(value, self.font, limit):
                    self._draw_mixed(img, draw, (value_x, text_y + j * line_pitch), part, self.font, (*TEXT_DARK, 255), (*label_color, 255))
                    j += 1
            y += row_h + ROW_GAP

        # brand border
        draw.rounded_rectangle(
            [ox, oy, ox + CARD_W - 1, oy + card_h - 1], RADIUS, outline=(*BORDER, 255), width=3
        )

        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()

    # ------------------------------------------------------------ pieces
    def _line_h(self) -> int:
        ascent, descent = self.font.getmetrics()
        return ascent + descent

    def _row_height(self, label: str, values: list[str], label_w: float) -> int:
        limit = CARD_W - PAD * 2 - ROW_PAD_X * 2 - label_w
        n = 0
        for value in values:
            n += len(self._wrap(value, self.font, limit))
        return ROW_PAD_V * 2 + n * self._line_h() + (n - 1) * LINE_GAP

    def _label_width(self, rows: list[list]) -> float:
        widths = [self._measure(label, self.font) for label, _ in rows if label]
        return max(widths) + 22 if widths else 0.0

    def _split_header(
        self, rows: list[list]
    ) -> tuple[str, str, list[list]]:
        title = ""
        subtitle = ""
        body: list[list] = []
        for label, values in rows:
            first = values[0] if values else ""
            if not title and label == "昵称":
                title = first
                continue
            if not subtitle and label == "QQ号":
                subtitle = first
                continue
            if not title and label in ("群昵称", "备注"):
                title = first
                continue
            body.append([label, values])
        if not title:
            title = "资料卡"
        if subtitle:
            subtitle = f"QQ {subtitle}"
        else:
            subtitle = "MoeMoe Profile Card"
        return title, subtitle, body

    def _load_avatar(self, avatar: bytes) -> Image.Image:
        img = Image.open(BytesIO(avatar)).convert("RGBA")
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        return img.resize((AVATAR_D, AVATAR_D), Image.LANCZOS)

    def _paint_header(self, card: Image.Image) -> None:
        header = Image.new("RGBA", (CARD_W, HEADER_H))
        hdraw = ImageDraw.Draw(header)
        for row in range(HEADER_H):
            hdraw.line(
                [(0, row), (CARD_W, row)],
                fill=(*_mix(HEADER_TOP, HEADER_BOTTOM, row / HEADER_H), 255),
            )
        # soft decorative blobs, like the logo background
        blob = Image.new("RGBA", (CARD_W, HEADER_H), (0, 0, 0, 0))
        bdraw = ImageDraw.Draw(blob)
        bdraw.ellipse([-120, -140, 260, 200], fill=(255, 255, 255, 46))
        bdraw.ellipse([CARD_W - 220, HEADER_H - 190, CARD_W + 140, HEADER_H + 160], fill=(255, 255, 255, 42))
        header.alpha_composite(blob)

        # square off the header's bottom edge so only the card's top corners round it
        mask = Image.new("L", (CARD_W, HEADER_H), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.rounded_rectangle([0, 0, CARD_W - 1, HEADER_H - 1], RADIUS, fill=255)
        mdraw.rectangle([0, RADIUS, CARD_W, HEADER_H], fill=255)
        card.paste(header, (0, 0), mask)

    def _paint_footer(self, card: Image.Image, card_h: int) -> None:
        footer = Image.new("RGBA", (CARD_W, FOOTER_H), (*FOOTER_BG, 255))
        mask = Image.new("L", (CARD_W, FOOTER_H), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.rounded_rectangle([0, 0, CARD_W - 1, FOOTER_H - 1], RADIUS, fill=255)
        mdraw.rectangle([0, 0, CARD_W, RADIUS], fill=255)
        card.paste(footer, (0, card_h - FOOTER_H), mask)

        fdraw = ImageDraw.Draw(card)
        font = self._font(FOOTER_SIZE)
        text = "萌萌资料卡 · MoeMoe Profile Card"
        text_w = self._measure(text, font)
        cx = (CARD_W - text_w) / 2
        cy = card_h - FOOTER_H + (FOOTER_H - self.font_size) / 2 - 2
        _cat_face(fdraw, cx - 44, card_h - FOOTER_H / 2, 17)
        fdraw.text((cx, cy), text, font=font, fill=(*FOOTER_TEXT, 255))
        _heart(fdraw, CARD_W / 2 + text_w / 2 + 40, card_h - FOOTER_H / 2, 11, (255, 111, 165, 220))

    def _paint_header_content(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        ox: int,
        oy: int,
        avatar_img: Image.Image,
        title: str,
        subtitle: str,
    ) -> None:
        hx, hy = ox, oy
        # avatar with white ring
        ax = hx + PAD + AVATAR_D / 2
        ay = hy + HEADER_H / 2
        ring_r = AVATAR_D / 2 + AVATAR_RING
        draw.ellipse(
            [ax - ring_r, ay - ring_r, ax + ring_r, ay + ring_r],
            fill=(255, 255, 255, 235),
        )
        mask = Image.new("L", (AVATAR_D, AVATAR_D), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, AVATAR_D - 1, AVATAR_D - 1], fill=255)
        img.paste(avatar_img, (int(ax - AVATAR_D / 2), int(ay - AVATAR_D / 2)), mask)

        # title with graceful shrink, then ellipsis truncation as a last resort
        text_x = hx + PAD + AVATAR_D + 52
        max_w = CARD_W - PAD - text_x - 60
        size = TITLE_SIZE
        while size > 40:
            font = self._font(size)
            if self._measure(title, font) <= max_w:
                break
            size -= 2
        font = self._font(size)
        if self._measure(title, font) > max_w:
            while title and self._measure(title + "…", font) > max_w:
                title = title[:-1]
            title += "…"
        ascent, descent = font.getmetrics()
        line_h = ascent + descent
        title_y = hy + HEADER_H / 2 - line_h - 6
        self._draw_mixed(img, draw, (text_x, title_y), title, font, (*TITLE_COLOR, 255))

        sub_font = self._font(SUBTITLE_SIZE)
        self._draw_mixed(img, draw, (text_x + 4, title_y + line_h + 14), subtitle, sub_font, (*SUBTITLE_COLOR, 255))

        # decorations
        sdraw = ImageDraw.Draw(img)
        _sparkle(sdraw, hx + CARD_W - 96, hy + 52, 20, (255, 255, 255, 210))
        _sparkle(sdraw, hx + CARD_W - 160, hy + 108, 13, (255, 243, 176, 220))
        _heart(sdraw, hx + CARD_W - 120, hy + HEADER_H - 56, 13, (255, 255, 255, 190))

    # ------------------------------------------------------------ placeholder
    def create_placeholder_avatar(self) -> bytes:
        """Deterministic cat avatar used when the real avatar cannot be fetched."""
        size = 640
        img = Image.new("RGBA", (size, size))
        draw = ImageDraw.Draw(img)
        for row in range(size):
            draw.line(
                [(0, row), (size, row)],
                fill=(*_mix((255, 231, 205), (255, 205, 160), row / size), 255),
            )
        _cat_face(draw, size / 2, size / 2 + 20, 190)
        out = BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
