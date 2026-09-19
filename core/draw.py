"""MoeMoe profile card renderer.

Draws the profile card in the same pastel style as the plugin logo, using
Resource Han Rounded (SIL OFL, see resource/OFL-License.txt):
pink gradient header with the avatar, nickname, QQ level badges and the
signature, a white body with uniform rows for the remaining fields, an
optional LLM comment block, and a branded footer with the fetch time.
"""

from datetime import datetime
from io import BytesIO
from pathlib import Path

import emoji
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .profile import BoxUserProfile

RESOURCE_DIR = Path(__file__).resolve().parent / "resource"
FONT_PATH = RESOURCE_DIR / "ResourceHanRoundedCN-Medium.woff2"
EMOJI_PATH = RESOURCE_DIR / "NotoColorEmoji.ttf"

# ---------------------------------------------------------------- palette
HEADER_TOP = (255, 224, 238)
HEADER_BOTTOM = (255, 163, 205)
CARD_BG = (255, 255, 255)
ROW_BG = (247, 243, 246)
LABEL_COLOR = (186, 108, 143)
TEXT_DARK = (72, 66, 82)
TITLE_COLOR = (82, 58, 78)
SIGN_COLOR = (129, 82, 108)
ANALYSIS_BG = (255, 246, 228)
ANALYSIS_LABEL = (196, 138, 46)
FOOTER_BG = (250, 236, 243)
FOOTER_TEXT = (176, 118, 148)
BORDER = (243, 216, 229)
SHADOW_COLOR = (190, 90, 140)

# per-card-type theme: header gradient / footer / border / accent ("" = normal query)
_TYPE_THEMES = {
    "": {"top": (255, 224, 238), "bottom": (255, 163, 205), "footer": (250, 236, 243),
         "border": (243, 216, 229), "accent": (214, 84, 146), "label": ""},
    "join": {"top": (216, 245, 226), "bottom": (158, 220, 188), "footer": (233, 246, 238),
             "border": (186, 227, 203), "accent": (56, 128, 92), "label": "新朋友"},
    "leave": {"top": (233, 230, 245), "bottom": (196, 190, 226), "footer": (238, 236, 247),
              "border": (213, 209, 235), "accent": (110, 98, 146), "label": "已退群"},
    "kick": {"top": (255, 217, 217), "bottom": (249, 158, 168), "footer": (252, 233, 234),
             "border": (247, 201, 205), "accent": (192, 72, 72), "label": "被踢出群"},
}


def _type_theme(card_type: str) -> dict:
    return _TYPE_THEMES.get(card_type) or _TYPE_THEMES[""]

# ---------------------------------------------------------------- layout
CARD_W = 1040
PAD = 56
AVATAR_D = 216
AVATAR_RING = 10
RADIUS = 48
FOOTER_H = 74
ROW_PAD_X = 26
ROW_PAD_V = 9
ROW_GAP = 14
LINE_GAP = 6
FONT_SIZE = 32
TITLE_SIZE = 54
LEVEL_SIZE = 26
CHIP_SIZE = 28
SUBTITLE_SIZE = 27
SIGN_SIZE = 26
ANALYSIS_SIZE = 28
FOOTER_SIZE = 24
HEADER_TOP_PAD = 42
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
    ear, inner = (255, 217, 160), (255, 170, 185)
    face, eye = (255, 224, 170), (110, 72, 60)
    blush, whisk = (255, 148, 170, 150), (150, 110, 90, 110)

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
    bw, bh = 34 * k, 22 * k
    draw.ellipse([cx - 84 * k, cy + 6 * k, cx - 84 * k + bw, cy + 6 * k + bh], fill=blush)
    draw.ellipse([cx + 84 * k - bw, cy + 6 * k, cx + 84 * k, cy + 6 * k + bh], fill=blush)
    ww = max(2, int(4 * k))
    draw.line([cx - 108 * k, cy - 12 * k, cx - 142 * k, cy - 22 * k], fill=whisk, width=ww)
    draw.line([cx - 108 * k, cy + 6 * k, cx - 142 * k, cy + 14 * k], fill=whisk, width=ww)
    draw.line([cx + 108 * k, cy - 12 * k, cx + 142 * k, cy - 22 * k], fill=whisk, width=ww)
    draw.line([cx + 108 * k, cy + 6 * k, cx + 142 * k, cy + 14 * k], fill=whisk, width=ww)


# ------------------------------------------------- QQ level icon painters
def _icon_crown(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    gold = (246, 184, 76, 255)
    d.polygon(
        [(0.08 * size, 0.78 * size), (0.02 * size, 0.28 * size), (0.28 * size, 0.48 * size),
         (0.5 * size, 0.12 * size), (0.72 * size, 0.48 * size), (0.98 * size, 0.28 * size),
         (0.92 * size, 0.78 * size)],
        fill=gold,
    )
    d.rounded_rectangle([0.08 * size, 0.82 * size, 0.92 * size, 0.94 * size], 2, fill=gold)
    return img


def _icon_sun(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = size / 2
    ray, r1, r2 = size * 0.10, size * 0.30, size * 0.47
    for i in range(8):
        a = i * 45
        x1 = c + r1 * _cos(a)
        y1 = c + r1 * _sin(a)
        x2 = c + r2 * _cos(a - 14)
        y2 = c + r2 * _sin(a - 14)
        x3 = c + r2 * _cos(a + 14)
        y3 = c + r2 * _sin(a + 14)
        d.polygon([(x1, y1), (x2, y2), (x3, y3)], fill=(247, 166, 76, 255))
    d.ellipse([c - size * 0.27, c - size * 0.27, c + size * 0.27, c + size * 0.27], fill=(250, 190, 96, 255))
    return img


def _cos(deg: float) -> float:
    import math

    return math.cos(math.radians(deg))


def _sin(deg: float) -> float:
    import math

    return math.sin(math.radians(deg))


def _icon_moon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0.12 * size, 0.08 * size, 0.88 * size, 0.92 * size], fill=(245, 205, 107, 255))
    # punch out an offset circle to form the crescent (PIL shapes write raw pixels)
    d.ellipse([0.34 * size, 0.0, 1.06 * size, 0.72 * size], fill=(0, 0, 0, 0))
    return img


def _icon_star(size: int) -> Image.Image:
    import math

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, r1, r2 = size / 2, size * 0.5, size * 0.21
    pts = []
    for i in range(10):
        r = r1 if i % 2 == 0 else r2
        a = -90 + i * 36
        pts.append((c + r * math.cos(math.radians(a)), c + r * math.sin(math.radians(a))))
    d.polygon(pts, fill=(246, 194, 76, 255))
    return img


ICON_PAINTERS = {
    "👑": _icon_crown,
    "🌞": _icon_sun,
    "🌙": _icon_moon,
    "⭐": _icon_star,
}


class CardMaker:
    """Renders QQ profile data into a moe-style profile card PNG."""

    def __init__(self, font_size: int = FONT_SIZE):
        self.font_size = font_size
        self.font = ImageFont.truetype(FONT_PATH, font_size)
        try:
            self.emoji_font = ImageFont.truetype(EMOJI_PATH, font_size)
        except OSError:
            self.emoji_font = ImageFont.truetype(EMOJI_PATH, 109)
        self._font_cache: dict[int, ImageFont.FreeTypeFont] = {font_size: self.font}
        self._emoji_tiles: dict[str, Image.Image | None] = {}
        # the bundled emoji font is subsetted; missing glyphs would render as
        # tofu boxes, so keep its cmap to detect and skip unsupported emoji
        try:
            from fontTools.ttLib import TTFont

            self._cmap: set[int] | None = set(TTFont(str(EMOJI_PATH)).getBestCmap().keys())
        except Exception:
            self._cmap = None

    # ------------------------------------------------------------ fonts
    def _font(self, size: int) -> ImageFont.FreeTypeFont:
        font = self._font_cache.get(size)
        if font is None:
            font = ImageFont.truetype(FONT_PATH, size)
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

    def _truncate(self, text: str, font: ImageFont.FreeTypeFont, limit: float, tail: str = "…") -> str:
        while text and self._measure(text + tail, font) > limit:
            text = text[:-1]
        return text + tail

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
                    if ch in ICON_PAINTERS:
                        tile = ICON_PAINTERS[ch](emoji_h)
                        ey = int(y + (ascent - emoji_h) / 2 + font.size * 0.06)
                        img.alpha_composite(tile, (int(x), max(0, ey)))
                        x += tile.width + 3
                        continue
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
    _EXTRA_LABELS = frozenset({"退群时间", "被踢时间", "在群时长", "操作管理员"})  # service-appended rows outside FIELD_LABELS

    @staticmethod
    def _parse_rows(lines: list[str]) -> list[list]:
        """Split display lines into [label, [value lines]] rows.

        Lines whose prefix before the full-width colon is a known field label
        start a row; everything else (library indents, wrapped signatures) is a
        continuation line of the previous row.
        """
        labels = set(BoxUserProfile.FIELD_LABELS.values()) | CardMaker._EXTRA_LABELS
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
    _CARD_TYPE_CHIPS = {
        "join": ("新朋友", (205, 240, 216), (56, 128, 92)),
        "leave": ("已退群", (226, 222, 240), (110, 98, 146)),
        "kick": ("被踢出群", (255, 212, 212), (190, 72, 72)),
    }

    def _chip_metrics(self, card_type: str) -> tuple[str, int, int, tuple] | None:
        """Header corner chip for event cards: (text, width, height, accent) or None."""
        theme = _type_theme(card_type)
        label = theme.get("label")
        if not label:
            return None
        font = self._font(CHIP_SIZE)
        return label, int(self._measure(label, font)) + 30, sum(font.getmetrics()) + 14, theme["accent"]

    def create(
        self,
        avatar: bytes,
        reply: list[str],
        level: str = "",
        join_rank: str = "",
        analyses: dict[str, str] | None = None,
        fetched_at: datetime | None = None,
        card_type: str = "",
        welcome_text: str = "",
        welcome_image: bytes | None = None,
    ) -> bytes:
        """Create a profile card PNG.

        Args:
            avatar: Avatar image bytes.
            reply: Display lines to render.
            level: QQ level badge text, e.g. "👑🌞(57)" or "等级隐藏".
            join_rank: Optional join-order text, e.g. "第 12 位 · 共 345 人".
            analyses: Optional AI comments keyed "avatar" / "signature" / "overall".
            fetched_at: When the profile data was fetched (shown in the footer).
            card_type: "" / "join" / "leave" / "kick" — draws the header corner chip.
            welcome_text: Optional welcome message rendered inside join cards.
            welcome_image: Optional pool image embedded inside join cards.

        Returns:
            Rendered PNG bytes.
        """
        rows = self._parse_rows(reply)
        title, qq, sig, body = self._split_header(rows)
        if join_rank:
            body.insert(0, ["入群排位", [join_rank]])

        blocks = self._analysis_blocks(analyses)

        body_h = BODY_TOP_PAD + BODY_BOTTOM_PAD
        label_w = self._label_width(body)
        if body:
            body_h += sum(self._row_height(label, values, label_w) for label, values in body)
            body_h += ROW_GAP * (len(body) - 1)

        analysis_h = sum(self._analysis_block_height(text) for _label, text in blocks)
        if len(blocks) > 1:
            analysis_h += ROW_GAP * (len(blocks) - 1)

        welcome_img, welcome_img_h = self._fit_welcome_image(welcome_image)
        welcome_h = self._welcome_height(welcome_text, welcome_img_h) if welcome_text else 0

        header_h = self._header_height(title, level, sig, card_type)
        card_h = header_h + welcome_h + analysis_h + body_h + FOOTER_H
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

        ox, oy = shadow_pad, shadow_pad
        theme = _type_theme(card_type)
        card = Image.new("RGBA", (CARD_W, card_h), (0, 0, 0, 0))
        cdraw = ImageDraw.Draw(card)
        cdraw.rounded_rectangle([0, 0, CARD_W - 1, card_h - 1], RADIUS, fill=(*CARD_BG, 255))
        self._paint_header(card, header_h, card_type)
        self._paint_footer(card, card_h, fetched_at, card_type)
        card.putalpha(_rounded_mask((CARD_W, card_h), RADIUS))
        img.alpha_composite(card, (ox, oy))

        draw = ImageDraw.Draw(img)
        avatar_img = self._load_avatar(avatar)
        self._paint_header_content(img, draw, ox, oy, avatar_img, title, qq, level, sig, header_h, card_type)

        y = oy + header_h + BODY_TOP_PAD
        if welcome_text:
            y = self._paint_welcome(img, draw, ox, y, welcome_text, welcome_img, card_type) + ROW_GAP
        for block_label, block_text in blocks:
            y = self._paint_analysis(img, draw, ox, y, block_label, block_text) + ROW_GAP

        row_x = ox + PAD
        row_w = CARD_W - PAD * 2
        for i, (label, values) in enumerate(body):
            row_h = self._row_height(label, values, label_w)
            draw.rounded_rectangle([row_x, y, row_x + row_w, y + row_h], 20, fill=(*ROW_BG, 255))
            text_y = y + ROW_PAD_V
            value_x = row_x + ROW_PAD_X + label_w
            if label:
                draw.text((row_x + ROW_PAD_X, text_y), label, font=self.font, fill=(*LABEL_COLOR, 255))
            limit = row_x + row_w - ROW_PAD_X - value_x
            line_pitch = self._line_h() + LINE_GAP
            j = 0
            for value in values:
                for part in self._wrap(value, self.font, limit):
                    self._draw_mixed(img, draw, (value_x, text_y + j * line_pitch), part, self.font, (*TEXT_DARK, 255), (*LABEL_COLOR, 255))
                    j += 1
            y += row_h + ROW_GAP

        # brand border
        draw.rounded_rectangle(
            [ox, oy, ox + CARD_W - 1, oy + card_h - 1], RADIUS, outline=(*theme["border"], 255), width=2
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

    def _split_header(self, rows: list[list]) -> tuple[str, str, str, list[list]]:
        """Pull nickname/QQ/signature out of the rows for the header.

        The QQ level badge is passed in structurally (not via display lines).
        The signature keeps its wrapped lines joined back into one string.
        """
        title = ""
        qq = ""
        sig = ""
        body: list[list] = []
        for label, values in rows:
            first = values[0] if values else ""
            if not title and label == "昵称":
                title = first
                continue
            if not qq and label == "QQ号":
                qq = first
                continue
            if not title and label in ("群昵称", "备注"):
                title = first
                continue
            if not sig and label == "签名":
                sig = "".join(values)
                continue
            body.append([label, values])
        if not title:
            title = "资料卡"
        return title, qq, sig, body

    def _level_metrics(self, level: str) -> dict:
        """Split the level text into icons + number and compute badge metrics.

        Icons live in a white pill (readable on the pink header); the number
        sits as plain dark text beside the pill. `total` includes the gap to
        the title, so the title layout can reserve exactly this much space.
        """
        level_font = self._font(LEVEL_SIZE)
        icons = "".join(ch for ch in level if ch in ICON_PAINTERS)
        rest = "".join(ch for ch in level if ch not in ICON_PAINTERS).strip()
        icon_h = self._emoji_target(level_font)
        pill_w = len(icons) * (icon_h + 3) + 24 if icons else 0  # 12px padding each side
        pill_h = icon_h + 10
        rest_w = self._measure(rest, level_font) if rest else 0.0
        gap = 10 if icons and rest else 0
        return {
            "font": level_font,
            "icons": icons,
            "rest": rest,
            "icon_h": icon_h,
            "pill_w": pill_w,
            "pill_h": pill_h,
            "rest_w": rest_w,
            "gap": gap,
            "total": 18 + pill_w + gap + rest_w,
        }

    def _fit_title(self, title: str, level: str, card_type: str = "") -> tuple[ImageFont.FreeTypeFont, str]:
        """Pick the largest title font (then truncate) that leaves room for the level badge and chip."""
        text_x = PAD + AVATAR_D + 52
        avail = CARD_W - PAD - text_x - 40
        chip = self._chip_metrics(card_type)
        reserve = (self._level_metrics(level)["total"] if level else 0) + (chip[1] + 16 if chip else 0)
        title_max = avail - reserve
        size = TITLE_SIZE
        while size > 40:
            if self._measure(title, self._font(size)) <= title_max:
                break
            size -= 2
        font = self._font(size)
        if self._measure(title, font) > title_max:
            title = self._truncate(title, font, title_max)
        return font, title

    def _header_height(self, title: str, level: str, sig: str, card_type: str = "") -> int:
        text_x = PAD + AVATAR_D + 52
        avail = CARD_W - PAD - text_x - 40
        title_font, _ = self._fit_title(title, level, card_type)
        title_h = sum(title_font.getmetrics())
        h = HEADER_TOP_PAD + title_h + 8 + sum(self._font(SUBTITLE_SIZE).getmetrics()) + 6
        if sig:
            sig_font = self._font(SIGN_SIZE)
            n = min(len(self._wrap(sig, sig_font, avail)), 2)
            h += 12 + n * (sum(sig_font.getmetrics()) + 4)
        return max(int(h) + 36, AVATAR_D + AVATAR_RING * 2 + 40)

    def _load_avatar(self, avatar: bytes) -> Image.Image:
        img = Image.open(BytesIO(avatar)).convert("RGBA")
        w, h = img.size
        side = min(w, h)
        img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        return img.resize((AVATAR_D, AVATAR_D), Image.LANCZOS)

    def _paint_header(self, card: Image.Image, header_h: int, card_type: str = "") -> None:
        theme = _type_theme(card_type)
        header = Image.new("RGBA", (CARD_W, header_h))
        hdraw = ImageDraw.Draw(header)
        for row in range(header_h):
            hdraw.line(
                [(0, row), (CARD_W, row)],
                fill=(*_mix(theme["top"], theme["bottom"], row / header_h), 255),
            )
        mask = Image.new("L", (CARD_W, header_h), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.rounded_rectangle([0, 0, CARD_W - 1, header_h - 1], RADIUS, fill=255)
        mdraw.rectangle([0, RADIUS, CARD_W, header_h], fill=255)
        card.paste(header, (0, 0), mask)

    def _paint_footer(self, card: Image.Image, card_h: int, fetched_at: datetime | None, card_type: str = "") -> None:
        theme = _type_theme(card_type)
        footer = Image.new("RGBA", (CARD_W, FOOTER_H), (*theme["footer"], 255))
        mask = Image.new("L", (CARD_W, FOOTER_H), 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.rounded_rectangle([0, 0, CARD_W - 1, FOOTER_H - 1], RADIUS, fill=255)
        mdraw.rectangle([0, 0, CARD_W, RADIUS], fill=255)
        card.paste(footer, (0, card_h - FOOTER_H), mask)

        fdraw = ImageDraw.Draw(card)
        font = self._font(FOOTER_SIZE)
        footer_text = theme["accent"] if card_type else FOOTER_TEXT
        cy = card_h - FOOTER_H + (FOOTER_H - sum(font.getmetrics())) / 2
        _cat_face(fdraw, PAD + 12, card_h - FOOTER_H / 2, 15)
        fdraw.text((PAD + 40, cy), "萌萌资料卡", font=font, fill=(*footer_text, 255))
        _heart(fdraw, PAD + 44 + self._measure("萌萌资料卡", font) + 14, card_h - FOOTER_H / 2, 8, (*footer_text, 200))
        if fetched_at:
            time_text = fetched_at.strftime("%Y-%m-%d %H:%M")
            tw = self._measure(time_text, font)
            fdraw.text((CARD_W - PAD - tw, cy), time_text, font=font, fill=(*footer_text, 235))

    def _paint_qq_digits(self, img: Image.Image, draw: ImageDraw.ImageDraw, x: float, y: float, qq: str) -> None:
        """Draw the QQ number as one cute tilted white square per digit.

        White digits-on-pink was unreadable; each digit now sits on its own
        solid white rounded tile, alternately tilted for the moe look.
        """
        label_font = self._font(SUBTITLE_SIZE)
        label_h = sum(label_font.getmetrics())
        draw.text((x, y), "QQ", font=label_font, fill=(*SIGN_COLOR, 255))
        x += self._measure("QQ", label_font) + 12

        digit_font = self._font(22)
        digit_ascent, digit_descent = digit_font.getmetrics()
        digit_lh = digit_ascent + digit_descent
        tile = 34
        center_y = y + label_h / 2
        for i, ch in enumerate(qq):
            if not ch.isdigit():
                continue
            tile_img = Image.new("RGBA", (tile + 8, tile + 8), (0, 0, 0, 0))
            tdraw = ImageDraw.Draw(tile_img)
            tdraw.rounded_rectangle([4, 4, 4 + tile, 4 + tile], 9, fill=(255, 255, 255, 255))
            char_w = digit_font.getlength(ch)
            tdraw.text(
                (4 + (tile - char_w) / 2, 4 + (tile - digit_lh) / 2),
                ch,
                font=digit_font,
                fill=(*TITLE_COLOR, 255),
            )
            tile_img = tile_img.rotate(4 if i % 2 else -4, resample=Image.BICUBIC)
            img.alpha_composite(tile_img, (int(x), int(center_y - tile_img.height / 2)))
            x += tile + 5

    def _paint_header_content(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        ox: int,
        oy: int,
        avatar_img: Image.Image,
        title: str,
        qq: str,
        level: str,
        sig: str,
        header_h: int,
        card_type: str = "",
    ) -> None:
        # avatar with white ring, vertically centered
        ax = ox + PAD + AVATAR_D / 2
        ay = oy + header_h / 2
        ring_r = AVATAR_D / 2 + AVATAR_RING
        draw.ellipse([ax - ring_r, ay - ring_r, ax + ring_r, ay + ring_r], fill=(255, 255, 255, 240))
        mask = Image.new("L", (AVATAR_D, AVATAR_D), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, AVATAR_D - 1, AVATAR_D - 1], fill=255)
        img.paste(avatar_img, (int(ax - AVATAR_D / 2), int(ay - AVATAR_D / 2)), mask)

        text_x = ox + PAD + AVATAR_D + 52
        avail = CARD_W - PAD - text_x - 40

        # title line: nickname + level badge
        title_font, title = self._fit_title(title, level, card_type)
        title_h = sum(title_font.getmetrics())
        title_y = oy + HEADER_TOP_PAD
        self._draw_mixed(img, draw, (text_x, title_y), title, title_font, (*TITLE_COLOR, 255))
        if level:
            m = self._level_metrics(level)
            title_center = title_y + title_h / 2
            lx = text_x + self._measure(title, title_font) + 18
            if m["icons"]:
                pill_y = title_center - m["pill_h"] / 2
                draw.rounded_rectangle(
                    [lx, pill_y, lx + m["pill_w"], pill_y + m["pill_h"]],
                    m["pill_h"] / 2,
                    fill=(255, 255, 255, 255),
                )
                ascent = m["font"].getmetrics()[0]
                icons_y = title_center - ascent / 2 - m["font"].size * 0.06
                self._draw_mixed(img, draw, (lx + 12, icons_y), m["icons"], m["font"], (*TITLE_COLOR, 255))
            if m["rest"]:
                rest_lh = sum(m["font"].getmetrics())
                rest_x = lx + m["pill_w"] + m["gap"] if m["icons"] else lx
                self._draw_mixed(
                    img, draw, (rest_x, title_center - rest_lh / 2), m["rest"], m["font"], (*TITLE_COLOR, 255)
                )

        # QQ line: "QQ" label + one cute tilted square per digit
        sub_y = title_y + title_h + 8
        if qq:
            self._paint_qq_digits(img, draw, text_x + 2, sub_y, qq)
        # signature line(s)
        if sig:
            sig_font = self._font(SIGN_SIZE)
            sig_y = sub_y + sum(self._font(SUBTITLE_SIZE).getmetrics()) + 12
            sig_lh = sum(sig_font.getmetrics())
            lines = self._wrap(sig, sig_font, avail)
            if len(lines) > 2:
                lines = lines[:2]
                lines[1] = self._truncate(lines[1], sig_font, avail)
            for i, part in enumerate(lines):
                self._draw_mixed(
                    img, draw,
                    (text_x + 2, sig_y + i * (sig_lh + 4)),
                    f"“{part}”" if len(lines) == 1 else (f"“{part}" if i == 0 else f"{part}”"),
                    sig_font, (*SIGN_COLOR, 255),
                )

        # corner chip for event cards, or the subtle sparkle on normal cards
        sdraw = ImageDraw.Draw(img)
        chip = self._chip_metrics(card_type)
        if chip:
            chip_text, chip_w, chip_h, accent = chip
            chip_x = CARD_W - PAD - chip_w
            chip_y = 36
            sdraw.rounded_rectangle(
                [ox + chip_x, oy + chip_y, ox + chip_x + chip_w, oy + chip_y + chip_h],
                chip_h / 2,
                fill=(255, 255, 255, 255),
                outline=(*accent, 255),
                width=2,
            )
            sdraw.text(
                (ox + chip_x + 15, oy + chip_y + 7), chip_text,
                font=self._font(CHIP_SIZE), fill=(*accent, 255),
            )
        else:
            _sparkle(sdraw, ox + CARD_W - 92, oy + 50, 18, (255, 255, 255, 200))

    _ANALYSIS_LABELS = (
        ("avatar", "头像印象 · AI"),
        ("signature", "签名解读 · AI"),
        ("overall", "综合锐评 · AI"),
    )

    def _analysis_blocks(self, analyses: dict[str, str] | None) -> list[tuple[str, str]]:
        analyses = analyses or {}
        blocks: list[tuple[str, str]] = []
        for key, label in self._ANALYSIS_LABELS:
            text = (analyses.get(key) or "").strip()
            if text:
                blocks.append((label, text))
        return blocks

    def _analysis_block_height(self, analysis: str) -> int:
        block_w = CARD_W - PAD * 2
        text_w_limit = block_w - ROW_PAD_X * 2
        label_font = self._font(FOOTER_SIZE)
        font = self._font(ANALYSIS_SIZE)
        label_h = sum(label_font.getmetrics()) + 6
        n = len(self._wrap(analysis, font, text_w_limit))
        return 2 * ROW_PAD_V + 12 + label_h + n * (sum(font.getmetrics()) + LINE_GAP)

    def _paint_analysis(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        ox: int,
        y: int,
        label: str,
        analysis: str,
    ) -> int:
        """Draw one AI comment block; returns the block's bottom y."""
        block_w = CARD_W - PAD * 2
        text_w_limit = block_w - ROW_PAD_X * 2
        label_font = self._font(FOOTER_SIZE)
        font = self._font(ANALYSIS_SIZE)
        label_h = sum(label_font.getmetrics())
        n = len(self._wrap(analysis, font, text_w_limit))
        block_h = 2 * ROW_PAD_V + 12 + label_h + 6 + n * (sum(font.getmetrics()) + LINE_GAP)

        draw.rounded_rectangle([ox + PAD, y, ox + PAD + block_w, y + block_h], 20, fill=(*ANALYSIS_BG, 255))
        ty = y + ROW_PAD_V + 12
        _heart(draw, ox + PAD + ROW_PAD_X + 7, ty + label_h / 2, 9, (255, 111, 165, 230))
        draw.text(
            (ox + PAD + ROW_PAD_X + 24, ty), label, font=label_font, fill=(*ANALYSIS_LABEL, 255)
        )
        ty += label_h + 6
        for part in self._wrap(analysis, font, text_w_limit):
            self._draw_mixed(img, draw, (ox + PAD + ROW_PAD_X, ty), part, font, (*TEXT_DARK, 255))
            ty += sum(font.getmetrics()) + LINE_GAP
        return y + block_h

    # ------------------------------------------------------------ 欢迎区
    def _fit_welcome_image(self, welcome_image: bytes | None) -> tuple[Image.Image | None, int]:
        """Scale the pool image to card width (cap 640px height) and round its corners."""
        if not welcome_image:
            return None, 0
        try:
            wimg = Image.open(BytesIO(welcome_image)).convert("RGBA")
            max_w = CARD_W - PAD * 2 - ROW_PAD_X * 2
            scale = min(max_w / wimg.width, 640 / wimg.height, 1.0)
            new_w = max(1, int(wimg.width * scale))
            new_h = max(1, int(wimg.height * scale))
            wimg = wimg.resize((new_w, new_h), Image.LANCZOS)
            wimg.putalpha(ImageChops.multiply(wimg.getchannel("A"), _rounded_mask((new_w, new_h), 16)))
            return wimg, new_h + 10
        except Exception:
            return None, 0

    def _welcome_height(self, welcome_text: str, welcome_img_h: int) -> int:
        block_w = CARD_W - PAD * 2
        text_limit = block_w - ROW_PAD_X * 2
        label_font = self._font(FOOTER_SIZE)
        font = self._font(FONT_SIZE)
        label_h = sum(label_font.getmetrics()) + 6
        n = 0
        for part in welcome_text.split("\n"):
            n += len(self._wrap(part, font, text_limit))
        return 2 * ROW_PAD_V + 12 + label_h + n * (sum(font.getmetrics()) + LINE_GAP) + welcome_img_h

    def _paint_welcome(
        self,
        img: Image.Image,
        draw: ImageDraw.ImageDraw,
        ox: int,
        y: int,
        welcome_text: str,
        welcome_img: Image.Image | None,
        card_type: str,
    ) -> int:
        """Draw the welcome block (text + optional pool image); returns bottom y."""
        theme = _type_theme(card_type)
        block_w = CARD_W - PAD * 2
        text_limit = block_w - ROW_PAD_X * 2
        label_font = self._font(FOOTER_SIZE)
        font = self._font(FONT_SIZE)
        label_h = sum(label_font.getmetrics())
        text_lines = []
        for part in welcome_text.split("\n"):
            text_lines.extend(self._wrap(part, font, text_limit))
        img_h = (welcome_img.height + 10) if welcome_img is not None else 0
        block_h = 2 * ROW_PAD_V + 12 + label_h + 6 + len(text_lines) * (sum(font.getmetrics()) + LINE_GAP) + img_h

        draw.rounded_rectangle([ox + PAD, y, ox + PAD + block_w, y + block_h], 20, fill=(*theme["footer"], 255))
        ty = y + ROW_PAD_V + 12
        _heart(draw, ox + PAD + ROW_PAD_X + 7, ty + label_h / 2, 9, (*theme["accent"], 230))
        draw.text(
            (ox + PAD + ROW_PAD_X + 24, ty), "欢迎你 · Welcome",
            font=label_font, fill=(*theme["accent"], 255),
        )
        ty += label_h + 6
        for part in text_lines:
            self._draw_mixed(img, draw, (ox + PAD + ROW_PAD_X, ty), part, font, (*TEXT_DARK, 255))
            ty += sum(font.getmetrics()) + LINE_GAP
        if welcome_img is not None:
            ix = ox + PAD + (block_w - welcome_img.width) // 2
            img.alpha_composite(welcome_img, (ix, ty))
        return y + block_h

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
