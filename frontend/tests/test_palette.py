"""Design tokens in ``styles.css`` meet WCAG contrast in both appearances.

The light palette is the first ``:root`` block, the dark palette the ``:root`` block inside
``@media (prefers-color-scheme: dark)``. Ratios follow WCAG 2.x (relative luminance):
text >= 4.5:1 (primary text >= 7:1, the Apple HIG target for custom pairs), UI boundaries
and fills >= 3:1.
"""

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parents[1] / "styles.css").read_text(encoding="utf-8")
TOKEN = re.compile(r"--fm-([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;")


def _tokens(block: str) -> dict[str, str]:
    return {name: value.upper() for name, value in TOKEN.findall(block)}


def _blocks() -> tuple[dict[str, str], dict[str, str]]:
    light_start = CSS.index(":root {")
    dark_start = CSS.index("@media (prefers-color-scheme: dark)")
    light = _tokens(CSS[light_start:dark_start])
    dark_block = CSS[dark_start : CSS.index("@media (prefers-contrast: more)")]
    return light, {**light, **_tokens(dark_block)}  # dark inherits what it does not override


LIGHT, DARK = _blocks()


def _luminance(color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio of two ``#RRGGBB`` colours."""
    high, low = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


#: (foreground token, background token, minimum ratio)
PAIRS = [
    ("text", "surface", 7.0),
    ("text", "canvas", 7.0),
    ("text-2", "surface", 7.0),
    ("text-2", "canvas", 4.5),
    ("text-3", "surface", 4.5),
    ("text-3", "canvas", 4.5),
    ("text-3", "surface-3", 4.5),  # chips
    ("control", "surface", 3.0),  # input and button borders
    ("brand", "surface", 4.5),
    ("brand", "canvas", 4.5),
    ("on-primary", "primary-bg", 4.5),
    ("on-primary", "primary-bg-hover", 4.5),
    ("brand-on-soft", "brand-soft", 4.5),
    ("ok-text", "ok-bg", 4.5),
    ("ok-text", "surface", 4.5),
    ("ok-fill", "surface", 3.0),
    ("warn-text", "warn-bg", 4.5),
    ("warn-text", "surface", 4.5),
    ("warn-fill", "surface", 3.0),
    ("bad-text", "bad-bg", 4.5),
    ("bad-text", "surface", 4.5),
    ("bad-fill", "surface", 3.0),
    ("bar", "surface", 3.0),  # chart bars are non-text graphics
]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize(("foreground", "background", "minimum"), PAIRS)
def test_pair_meets_wcag(scheme: str, foreground: str, background: str, minimum: float) -> None:
    tokens = LIGHT if scheme == "light" else DARK
    value = ratio(tokens[foreground], tokens[background])
    assert value >= minimum, f"{scheme}: {foreground} {tokens[foreground]} on {background} {tokens[background]} = {value:.2f}:1 < {minimum}:1"


def test_dark_palette_overrides_every_colour_of_the_light_palette() -> None:
    """A colour missing in the dark block would silently stay light in dark mode."""
    dark_start = CSS.index("@media (prefers-color-scheme: dark)")
    overridden = set(_tokens(CSS[dark_start : CSS.index("@media (prefers-contrast: more)")]))
    forgotten = {name for name in LIGHT if name not in overridden}
    # these are the same in both appearances on purpose
    assert forgotten <= set(), f"not overridden for dark mode: {sorted(forgotten)}"


def test_dark_surfaces_are_lighter_than_the_canvas() -> None:
    """HIG dark mode: elevated surfaces are brighter than the base layer."""
    assert _luminance(DARK["surface"]) > _luminance(DARK["canvas"])
    assert _luminance(DARK["surface-2"]) > _luminance(DARK["surface"])
    assert _luminance(DARK["surface-3"]) > _luminance(DARK["surface-2"])


def test_no_raw_angle_bracket_in_the_stylesheet() -> None:
    """The HTML sanitiser drops a style block that contains ``<`` (see docs/DESIGN.md)."""
    code = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    assert "<" not in code
