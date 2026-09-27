"""The square image attached to each LinkedIn post: an attention label, the
employer's logo on a panel, the job title, company, location and pay, and the
site's address along the bottom.

Rendered with Pillow and the bundled Open Sans (SIL Open Font License, see
fonts/OFL.txt), so it costs nothing and looks the same locally and on the
runner. Colour themes rotate between posts so the company page doesn't show the
same card over and over.
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

FONT_PATH = Path(__file__).parent / "fonts" / "OpenSans.ttf"

SIZE = 1200
MARGIN = 100
TEXT_WIDTH = SIZE - 2 * MARGIN
FOOTER_HEIGHT = 120
PANEL_BOX = (200, 204, 1000, 524)
LOGO_MAX = (640, 220)
MAX_LOGO_UPSCALE = 3.0

TITLE_SIZES = (76, 68)

FOOTER_TEXT = "PropertyManagementJobs.us"
LABELS = ("NEW JOB", "NOW HIRING")


@dataclass(frozen=True)
class Theme:
    name: str
    background: str
    title: str
    company: str
    details: str
    label_fill: str
    label_text: str
    footer_fill: str
    footer_text: str
    panel: str = "#ffffff"
    panel_border: str | None = None


# Coral and slate are the site's own colours (--primary-color family and the
# page text colour). Every theme keeps them so the posts still read as one brand.
THEMES = (
    Theme("slate", "#1e293b", "#ffffff", "#cbd5e1", "#ff8a73",
          "#f84f34", "#ffffff", "#f84f34", "#ffffff"),
    Theme("coral", "#f84f34", "#ffffff", "#fff1ed", "#ffffff",
          "#1e293b", "#ffffff", "#1e293b", "#ffffff"),
    Theme("cream", "#fbf5ee", "#1e293b", "#64748b", "#c9391f",
          "#f84f34", "#ffffff", "#1e293b", "#ffffff", panel_border="#eadfd3"),
    Theme("teal", "#0f3b46", "#ffffff", "#b9d6dc", "#ff8a73",
          "#f84f34", "#ffffff", "#f84f34", "#ffffff"),
    Theme("plum", "#3a1f4b", "#ffffff", "#dccbe6", "#ff8a73",
          "#f84f34", "#ffffff", "#f84f34", "#ffffff"),
)


@lru_cache(maxsize=None)
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(str(FONT_PATH), size)
    font.set_variation_by_name(weight)
    return font


_MEASURE = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _width(text: str, font: ImageFont.FreeTypeFont) -> float:
    return _MEASURE.textlength(text, font=font)


def _balanced_lines(text: str, font: ImageFont.FreeTypeFont) -> list[str] | None:
    """One line if it fits, else the two-line split with the most even line
    lengths - so "Market Property Manager III" never strands "III" on its own."""
    words = text.split()
    if _width(text, font) <= TEXT_WIDTH:
        return [text]
    best: list[str] | None = None
    best_width = float("inf")
    for cut in range(1, len(words)):
        lines = [" ".join(words[:cut]), " ".join(words[cut:])]
        widest = max(_width(line, font) for line in lines)
        if widest <= TEXT_WIDTH and widest < best_width:
            best, best_width = lines, widest
    return best


def _title_layout(title: str) -> tuple[int, list[str]] | None:
    for size in TITLE_SIZES:
        lines = _balanced_lines(title, _font("Bold", size))
        if lines:
            return size, lines
    return None


def title_fits(title: str) -> bool:
    """Whether the title renders in at most two lines at a readable size. The
    picker skips jobs whose title doesn't, rather than shrinking it to fit."""
    return _title_layout(title) is not None


# --- logos -------------------------------------------------------------------

def _content_box(logo: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of the logo's actual artwork, ignoring transparent or
    near-white padding. Several logo files are mostly empty margin."""
    r, g, b, a = logo.split()
    opaque = a.point(lambda v: 255 if v >= 24 else 0)
    not_white = ImageChops.lighter(
        ImageChops.lighter(r.point(lambda v: 255 if v <= 242 else 0),
                           g.point(lambda v: 255 if v <= 242 else 0)),
        b.point(lambda v: 255 if v <= 242 else 0),
    )
    return ImageChops.darker(opaque, not_white).getbbox()


def load_logo(path: Path) -> Image.Image:
    logo = Image.open(path).convert("RGBA")
    box = _content_box(logo)
    if box:
        logo = logo.crop(box)
    return logo


def logo_problem(path: Path) -> str | None:
    """A reason to skip this logo, or None if it is usable on a white panel.

    Catches files whose artwork is missing, like a white wordmark flattened onto
    a white background, where only a coloured accent survives."""
    if not path.exists():
        return "logo file missing"
    if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
        return f"unsupported logo format {path.suffix}"
    logo = Image.open(path).convert("RGBA")
    box = _content_box(logo)
    if box is None:
        return "logo is blank"
    width, height = box[2] - box[0], box[3] - box[1]
    if width < 24 or height < 12:
        return "logo artwork is too small"
    if width / height < 0.45:
        return "logo artwork looks cut off (tall sliver)"
    return None


# --- drawing -----------------------------------------------------------------

def _centered(draw: ImageDraw.ImageDraw, y: float, text: str, font, fill: str) -> None:
    x = (SIZE - _width(text, font)) / 2
    draw.text((x, y), text, font=font, fill=fill)


def _fit_font(text: str, weight: str, sizes: tuple[int, ...]) -> ImageFont.FreeTypeFont:
    for size in sizes:
        font = _font(weight, size)
        if _width(text, font) <= TEXT_WIDTH:
            return font
    return _font(weight, sizes[-1])


def render_card(
    *,
    title: str,
    company: str,
    location: str,
    pay: str,
    logo_path: Path,
    theme: Theme,
    label: str,
    out_path: Path,
) -> Path:
    layout = _title_layout(title)
    if layout is None:
        raise ValueError(f"title does not fit the card: {title!r}")
    title_size, title_lines = layout

    image = Image.new("RGB", (SIZE, SIZE), theme.background)
    draw = ImageDraw.Draw(image)

    # label pill
    label_font = _font("Bold", 40)
    label_width = _width(label, label_font)
    left = (SIZE - label_width) / 2 - 44
    draw.rounded_rectangle([left, 84, left + label_width + 88, 160], radius=38, fill=theme.label_fill)
    _centered(draw, 92, label, label_font, theme.label_text)

    # logo panel
    draw.rounded_rectangle(
        PANEL_BOX, radius=28, fill=theme.panel,
        outline=theme.panel_border, width=3 if theme.panel_border else 0,
    )
    logo = load_logo(logo_path)
    scale = min(LOGO_MAX[0] / logo.width, LOGO_MAX[1] / logo.height, MAX_LOGO_UPSCALE)
    logo = logo.resize((max(1, round(logo.width * scale)), max(1, round(logo.height * scale))), Image.LANCZOS)
    x0, y0, x1, y1 = PANEL_BOX
    image.paste(logo, ((x0 + x1 - logo.width) // 2, (y0 + y1 - logo.height) // 2), logo)

    # title, company, location and pay, centred as one block under the panel
    title_font = _font("Bold", title_size)
    title_step = round(title_size * 1.2)
    company_font = _fit_font(company, "SemiBold", (40, 36, 32))
    details_font = _font("SemiBold", 44)
    details = [f"{location}   •   {pay}"]
    if _width(details[0], details_font) > TEXT_WIDTH:
        details = [location, pay]

    block = [(line, title_font, theme.title, title_step) for line in title_lines]
    block.append((company, company_font, theme.company, 58))
    block += [(line, details_font, theme.details, 60) for line in details]
    gaps = 14 + 6  # after the title, after the company
    block_height = sum(step for *_, step in block) + gaps

    top, bottom = PANEL_BOX[3] + 40, SIZE - FOOTER_HEIGHT - 40
    y = top + max(0, (bottom - top - block_height) / 2)
    for index, (text, font, fill, step) in enumerate(block):
        _centered(draw, y, text, font, fill)
        y += step
        if index == len(title_lines) - 1:
            y += 14
        elif index == len(title_lines):
            y += 6

    # footer
    draw.rectangle([0, SIZE - FOOTER_HEIGHT, SIZE, SIZE], fill=theme.footer_fill)
    _centered(draw, SIZE - FOOTER_HEIGHT + 22, FOOTER_TEXT, _font("Bold", 50), theme.footer_text)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path, optimize=True)
    return out_path
