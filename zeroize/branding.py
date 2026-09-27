"""The Zeroize visual identity, in one place.

Both renderers - the GTK interface and the PDF certificate - draw from these
constants, so the application and the document it produces cannot drift apart.
Colours are stored as ``#rrggbb`` strings because that is what GTK CSS wants;
:func:`rgb` converts to the 0-1 float triple ReportLab wants.

The palette:

==========  =========  ====================================================
Token       Hex        Used for
==========  =========  ====================================================
charcoal    #16181D    Page ground, header bar, primary text on light
orange      #FF6B1A    The accent. The slash through the zero, selection,
                       progress fill, and the certificate's rules.
steel       #7A8794    Secondary text, labels, disabled states
mist        #EAEDF1    Light surfaces, table banding
paper       #FFFFFF    Certificate ground
verdant     #22C55E    Success - an "Erased" result, a passed verification
signal      #EF4444    Failure - a failed pass, a refused command
amber       #F59E0B    Caution - a supported but inadvisable choice
==========  =========  ====================================================

The mark is a slashed zero, the glyph long used to mean zeroized key material.
:func:`draw_mark` renders it as vector geometry rather than a bitmap so it is
crisp at the 16 px of a window icon and at the 64 pt of a certificate header,
and so the package carries no binary image asset that could drift from this
definition. :func:`mark_svg` emits the same geometry as SVG for the icon files
the installer places in the system icon theme.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------

CHARCOAL = "#16181D"
ORANGE = "#FF6B1A"
STEEL = "#7A8794"
MIST = "#EAEDF1"
PAPER = "#FFFFFF"
VERDANT = "#22C55E"
SIGNAL = "#EF4444"
AMBER = "#F59E0B"

#: Slightly lifted charcoal, for cards sitting on the charcoal ground.
CHARCOAL_RAISED = "#1F232B"
#: Muted orange for large fills, where full-strength accent would shout.
ORANGE_MUTED = "#C4551A"

#: Typeface preferences, most wanted first. Both renderers walk the list and
#: take the first one present, falling back to the toolkit default.
DISPLAY_FONT_STACK = ("Inter", "Roboto", "Cantarell", "DejaVu Sans", "Helvetica")
MONO_FONT_STACK = ("JetBrains Mono", "Roboto Mono", "Source Code Pro", "DejaVu Sans Mono", "Courier")


def rgb(hex_colour: str) -> tuple[float, float, float]:
    """Convert ``#rrggbb`` to the 0-1 float triple ReportLab and Cairo expect."""
    text = hex_colour.lstrip("#")
    return tuple(int(text[index : index + 2], 16) / 255 for index in (0, 2, 4))  # type: ignore[return-value]


def with_alpha(hex_colour: str, alpha: float) -> str:
    """Return ``#rrggbbaa`` - GTK CSS accepts eight-digit hex, ReportLab does not."""
    return f"{hex_colour}{int(max(0.0, min(1.0, alpha)) * 255):02x}"


# --------------------------------------------------------------------------
# The mark
# --------------------------------------------------------------------------

#: Geometry of the mark, expressed in a 100x100 box so both renderers can
#: scale it by a single factor. The zero is an ellipse ring; the slash runs
#: corner to corner through it, and is drawn with a rounded cap.
MARK_VIEWBOX = 100.0
_RING_CENTRE = (50.0, 50.0)
_RING_RADIUS_X = 30.0
_RING_RADIUS_Y = 38.0
_RING_WIDTH = 11.0
_SLASH_FROM = (26.0, 18.0)
_SLASH_TO = (74.0, 82.0)
_SLASH_WIDTH = 11.0


#: How much of a backed tile the glyph occupies. Icon guidelines across all
#: three desktop environments expect a keyline inset rather than a mark that
#: runs to the edge, and on a rounded tile an un-inset glyph collides with the
#: corner radius. A transparent mark has no tile, so it is not inset.
_TILE_GLYPH_SCALE = 0.74


def mark_svg(
    size: int = 128,
    *,
    ring_colour: str = PAPER,
    slash_colour: str = ORANGE,
    background: str | None = CHARCOAL,
    corner_radius: float = 22.0,
) -> str:
    """Return the mark as a standalone SVG document.

    Used by the build to generate the icon theme files. ``background`` of
    ``None`` produces a transparent mark for use on an existing surface, drawn
    at full size; with a background the glyph is inset inside the tile.
    """
    ground = ""
    glyph_transform = ""
    if background is not None:
        ground = (
            f'  <rect x="0" y="0" width="{MARK_VIEWBOX:g}" height="{MARK_VIEWBOX:g}" '
            f'rx="{corner_radius:g}" ry="{corner_radius:g}" fill="{background}"/>\n'
        )
        offset = MARK_VIEWBOX * (1 - _TILE_GLYPH_SCALE) / 2
        glyph_transform = f' transform="translate({offset:g} {offset:g}) scale({_TILE_GLYPH_SCALE:g})"'

    centre_x, centre_y = _RING_CENTRE
    slash_x1, slash_y1 = _SLASH_FROM
    slash_x2, slash_y2 = _SLASH_TO

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {MARK_VIEWBOX:g} {MARK_VIEWBOX:g}">\n'
        f"{ground}"
        f"  <g{glyph_transform}>\n"
        f'    <ellipse cx="{centre_x:g}" cy="{centre_y:g}" '
        f'rx="{_RING_RADIUS_X:g}" ry="{_RING_RADIUS_Y:g}" '
        f'fill="none" stroke="{ring_colour}" stroke-width="{_RING_WIDTH:g}"/>\n'
        f'    <line x1="{slash_x1:g}" y1="{slash_y1:g}" x2="{slash_x2:g}" y2="{slash_y2:g}" '
        f'stroke="{slash_colour}" stroke-width="{_SLASH_WIDTH:g}" stroke-linecap="round"/>\n'
        f"  </g>\n"
        f"</svg>\n"
    )


def draw_mark(
    canvas,
    origin_x: float,
    origin_y: float,
    size: float,
    *,
    ring_colour: str = CHARCOAL,
    slash_colour: str = ORANGE,
    background: str | None = None,
) -> None:
    """Draw the mark onto a ReportLab canvas with its lower-left at the origin.

    Kept here rather than in the certificate module so the logo has exactly one
    definition. ``canvas`` is a ``reportlab.pdfgen.canvas.Canvas``; the import
    stays inside the certificate package so this module has no hard dependency
    on ReportLab.
    """
    scale = size / MARK_VIEWBOX

    def point(x_value: float, y_value: float) -> tuple[float, float]:
        # SVG's y axis grows downward, PDF's grows upward.
        return origin_x + x_value * scale, origin_y + (MARK_VIEWBOX - y_value) * scale

    canvas.saveState()

    if background is not None:
        canvas.setFillColor(background)
        canvas.roundRect(origin_x, origin_y, size, size, 22.0 * scale, stroke=0, fill=1)

    centre_x, centre_y = point(*_RING_CENTRE)
    canvas.setStrokeColor(ring_colour)
    canvas.setLineWidth(_RING_WIDTH * scale)
    canvas.ellipse(
        centre_x - _RING_RADIUS_X * scale,
        centre_y - _RING_RADIUS_Y * scale,
        centre_x + _RING_RADIUS_X * scale,
        centre_y + _RING_RADIUS_Y * scale,
        stroke=1,
        fill=0,
    )

    canvas.setStrokeColor(slash_colour)
    canvas.setLineWidth(_SLASH_WIDTH * scale)
    canvas.setLineCap(1)  # round
    canvas.line(*point(*_SLASH_FROM), *point(*_SLASH_TO))

    canvas.restoreState()


def wordmark_letterspacing(font_size: float) -> float:
    """Tracking for the ZEROIZE wordmark, which is set wide and uppercase."""
    return font_size * 0.18


# --------------------------------------------------------------------------
# Boot-time artwork
# --------------------------------------------------------------------------
# The live image shows the brand twice before the desktop appears: the GRUB
# menu background and the Plymouth splash. Both are generated from the same
# geometry as the application icon, so there is still one definition of the
# mark and nothing to keep in sync by hand.


def splash_svg(
    width: int = 1024,
    height: int = 768,
    *,
    tagline: str = "",
    footer: str = "",
) -> str:
    """Background for the GRUB boot menu.

    The menu itself is drawn by GRUB over the lower half - live-build's theme
    places it at 52% of the height - so everything here stays in the upper
    portion and the lower half is left as clean ground for the entries.
    """
    mark_size = height * 0.20
    mark_x = (width - mark_size) / 2
    mark_y = height * 0.10

    wordmark_size = height * 0.075
    wordmark_y = mark_y + mark_size + height * 0.085
    tagline_y = wordmark_y + height * 0.048

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f'  <rect width="{width}" height="{height}" fill="{CHARCOAL}"/>',
        # A restrained accent rule under the wordmark, rather than a gradient
        # that would band badly at the 8-bit depth some firmware gives GRUB.
        f'  <rect x="{width * 0.5 - width * 0.06:g}" y="{tagline_y + height * 0.028:g}" '
        f'width="{width * 0.12:g}" height="2" fill="{ORANGE}"/>',
        "  <g>",
        _mark_group(mark_x, mark_y, mark_size, ring_colour=PAPER, slash_colour=ORANGE),
        "  </g>",
        f'  <text x="{width / 2:g}" y="{wordmark_y:g}" text-anchor="middle" '
        f'font-family="{DISPLAY_FONT_STACK[0]}, sans-serif" font-size="{wordmark_size:g}" '
        f'font-weight="bold" letter-spacing="{wordmark_size * 0.18:g}" '
        f'fill="{PAPER}">ZEROIZE</text>',
    ]

    if tagline:
        parts.append(
            f'  <text x="{width / 2:g}" y="{tagline_y:g}" text-anchor="middle" '
            f'font-family="{DISPLAY_FONT_STACK[0]}, sans-serif" '
            f'font-size="{height * 0.026:g}" fill="{STEEL}">{tagline}</text>'
        )

    if footer:
        parts.append(
            f'  <text x="{width / 2:g}" y="{height - height * 0.035:g}" text-anchor="middle" '
            f'font-family="{DISPLAY_FONT_STACK[0]}, sans-serif" '
            f'font-size="{height * 0.020:g}" fill="{STEEL}">{footer}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def plymouth_logo_svg(size: int = 320) -> str:
    """The mark alone, transparent, for the Plymouth boot splash.

    No tile and no background: Plymouth composites it over its own ground, and
    a tile would show as a hard square against the charcoal.
    """
    return mark_svg(size, background=None, ring_colour=PAPER, slash_colour=ORANGE)


def spinner_svg(size: int = 96, spokes: int = 12) -> str:
    """A pinwheel spinner, transparent, for the Plymouth boot splash.

    Drawn as discrete spokes with a graduated opacity ramp rather than as an
    arc with a gradient stroke. Plymouth animates this by rotating the whole
    image a fixed step each frame, and a spoked wheel lands exactly on its own
    geometry every step - so the ramp appears to travel round the ring with no
    seam. A gradient arc rotated the same way shows its start and end meeting.

    The spoke count and the rotation step in the Plymouth script are the same
    number for that reason; changing one without the other reintroduces the
    seam.
    """
    centre = size / 2.0
    outer = size * 0.46
    inner = size * 0.26
    width = max(2.0, size * 0.075)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">',
    ]
    for index in range(spokes):
        angle = 360.0 * index / spokes
        # Brightest spoke leads; the rest fall away behind it.
        opacity = 0.12 + 0.88 * (index / (spokes - 1)) ** 2
        parts.append(
            f'  <line x1="{centre:g}" y1="{centre - inner:g}" '
            f'x2="{centre:g}" y2="{centre - outer:g}" '
            f'stroke="{ORANGE}" stroke-width="{width:g}" stroke-linecap="round" '
            f'stroke-opacity="{opacity:.3f}" '
            f'transform="rotate({angle:g} {centre:g} {centre:g})"/>'
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def boot_wordmark_svg(width: int = 640, tagline: str = "") -> str:
    """The name, and optionally the tagline, as a transparent strip.

    Plymouth's own ``Image.Text`` renders with whatever font the initramfs
    happens to carry, at whatever size it chooses, with no tracking control -
    so the wordmark is rendered here instead and composited as an image. It is
    then identical to the wordmark on the GRUB splash, which is the screen
    immediately before it.
    """
    wordmark_size = width * 0.115
    height = int(width * (0.30 if tagline else 0.20))
    wordmark_y = height * (0.52 if tagline else 0.68)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        f'  <text x="{width / 2:g}" y="{wordmark_y:g}" text-anchor="middle" '
        f'font-family="{DISPLAY_FONT_STACK[0]}, sans-serif" font-size="{wordmark_size:g}" '
        f'font-weight="bold" letter-spacing="{wordmark_letterspacing(wordmark_size):g}" '
        f'fill="{PAPER}">ZEROIZE</text>',
    ]
    if tagline:
        parts.append(
            f'  <text x="{width / 2:g}" y="{height * 0.80:g}" text-anchor="middle" '
            f'font-family="{DISPLAY_FONT_STACK[0]}, sans-serif" '
            f'font-size="{width * 0.036:g}" letter-spacing="{width * 0.004:g}" '
            f'fill="{STEEL}">{tagline}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def wallpaper_svg(
    width: int = 1920,
    height: int = 1080,
    *,
    tagline: str = "",
    footer: str = "",
) -> str:
    """Desktop background, in the same visual language as the boot screens.

    Differs from :func:`splash_svg` in composition, not in palette: nothing is
    reserved for a menu, so the mark is centred, and the whole thing is held
    back a stop. A desktop is looked at for an hour while drives erase, and
    artwork that works for two seconds under GRUB becomes tiring at that
    length - so the mark is smaller, the accent rule thinner, and a soft
    vignette keeps window edges legible against it.
    """
    mark_size = height * 0.16
    mark_x = (width - mark_size) / 2
    mark_y = height * 0.33

    wordmark_size = height * 0.055
    wordmark_y = mark_y + mark_size + height * 0.085
    tagline_y = wordmark_y + height * 0.042

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "  <defs>",
        '    <radialGradient id="vignette" cx="50%" cy="42%" r="72%">',
        f'      <stop offset="0%" stop-color="{CHARCOAL_RAISED}"/>',
        f'      <stop offset="100%" stop-color="{CHARCOAL}"/>',
        "    </radialGradient>",
        "  </defs>",
        f'  <rect width="{width}" height="{height}" fill="url(#vignette)"/>',
        "  <g opacity=\"0.92\">",
        _mark_group(mark_x, mark_y, mark_size, ring_colour=PAPER, slash_colour=ORANGE),
        "  </g>",
        f'  <text x="{width / 2:g}" y="{wordmark_y:g}" text-anchor="middle" '
        f'font-family="{DISPLAY_FONT_STACK[0]}, sans-serif" font-size="{wordmark_size:g}" '
        f'font-weight="bold" letter-spacing="{wordmark_letterspacing(wordmark_size):g}" '
        f'fill="{PAPER}" opacity="0.92">ZEROIZE</text>',
        f'  <rect x="{width * 0.5 - width * 0.035:g}" y="{tagline_y + height * 0.022:g}" '
        f'width="{width * 0.07:g}" height="2" fill="{ORANGE}" opacity="0.8"/>',
    ]
    if tagline:
        parts.append(
            f'  <text x="{width / 2:g}" y="{tagline_y:g}" text-anchor="middle" '
            f'font-family="{DISPLAY_FONT_STACK[0]}, sans-serif" '
            f'font-size="{height * 0.022:g}" fill="{STEEL}">{tagline}</text>'
        )
    if footer:
        parts.append(
            f'  <text x="{width / 2:g}" y="{height - height * 0.045:g}" text-anchor="middle" '
            f'font-family="{DISPLAY_FONT_STACK[0]}, sans-serif" '
            f'font-size="{height * 0.017:g}" fill="{STEEL}" opacity="0.7">{footer}</text>'
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def banner_svg(
    width: int = 1600,
    height: int = 420,
    *,
    tagline: str = "",
) -> str:
    """Horizontal lockup for a README or a project page.

    A landscape banner rather than the stacked composition the boot screens
    use: a README is read at the top of a scrolling page, where a tall header
    pushes the actual content below the fold.

    The charcoal ground is deliberate. A transparent banner has to work against
    both GitHub themes and ends up compromising for each; an opaque one owns
    its rectangle and reads identically in light and dark.
    """
    mark_size = height * 0.46
    mark_x = width * 0.20
    mark_y = (height - mark_size) / 2

    text_x = mark_x + mark_size + width * 0.035
    wordmark_size = height * 0.26
    wordmark_y = height / 2 + (wordmark_size * 0.16 if tagline else wordmark_size * 0.35)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        "  <defs>",
        '    <linearGradient id="ground" x1="0" y1="0" x2="1" y2="1">',
        f'      <stop offset="0%" stop-color="{CHARCOAL_RAISED}"/>',
        f'      <stop offset="100%" stop-color="{CHARCOAL}"/>',
        "    </linearGradient>",
        "  </defs>",
        f'  <rect width="{width}" height="{height}" fill="url(#ground)"/>',
        # A single accent rule along the bottom edge, so the banner has a
        # defined end rather than fading into the page.
        f'  <rect x="0" y="{height - 6}" width="{width}" height="6" fill="{ORANGE}"/>',
        _mark_group(mark_x, mark_y, mark_size, ring_colour=PAPER, slash_colour=ORANGE),
        f'  <text x="{text_x:g}" y="{wordmark_y:g}" '
        f'font-family="{DISPLAY_FONT_STACK[0]}, sans-serif" font-size="{wordmark_size:g}" '
        f'font-weight="bold" letter-spacing="{wordmark_letterspacing(wordmark_size):g}" '
        f'fill="{PAPER}">ZEROIZE</text>',
    ]

    if tagline:
        parts.append(
            f'  <text x="{text_x:g}" y="{wordmark_y + height * 0.155:g}" '
            f'font-family="{DISPLAY_FONT_STACK[0]}, sans-serif" '
            f'font-size="{height * 0.088:g}" letter-spacing="{height * 0.006:g}" '
            f'fill="{STEEL}">{tagline}</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _mark_group(origin_x: float, origin_y: float, size: float, *, ring_colour: str, slash_colour: str) -> str:
    """The mark as SVG elements placed at an arbitrary position and scale."""
    scale = size / MARK_VIEWBOX
    centre_x, centre_y = _RING_CENTRE
    slash_x1, slash_y1 = _SLASH_FROM
    slash_x2, slash_y2 = _SLASH_TO
    return (
        f'    <g transform="translate({origin_x:g} {origin_y:g}) scale({scale:g})">\n'
        f'      <ellipse cx="{centre_x:g}" cy="{centre_y:g}" '
        f'rx="{_RING_RADIUS_X:g}" ry="{_RING_RADIUS_Y:g}" '
        f'fill="none" stroke="{ring_colour}" stroke-width="{_RING_WIDTH:g}"/>\n'
        f'      <line x1="{slash_x1:g}" y1="{slash_y1:g}" x2="{slash_x2:g}" y2="{slash_y2:g}" '
        f'stroke="{slash_colour}" stroke-width="{_SLASH_WIDTH:g}" stroke-linecap="round"/>\n'
        f"    </g>"
    )
