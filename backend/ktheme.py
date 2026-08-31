"""Semantic KColorScheme colours for the Qt frontends.

QPalette has no semantic roles, so a hardcoded `#c0392b` stays that red under
every colour scheme the user picks. KColorScheme has the roles and no Python
bindings, so read the same ini KConfig reads, in the same XDG order, and fall
back to Breeze. The four accents below are byte-identical in
BreezeLight.colors and BreezeDark.colors, so a constant is a safe fallback for
them; ForegroundInactive is not, because it tracks light against dark, so it is
resolved against the palette under a contrast floor (see _inactive).

Measured over every scheme installed here (BreezeLight, BreezeDark,
BreezeClassic), `[Colors:View]`: ForegroundPositive, ForegroundNegative,
ForegroundNeutral and DecorationFocus are byte-identical in all three.
DecorationHover is not -- BreezeClassic uses 147,206,233 -- so the constant
below is the Breeze Light/Dark value and is only ever reached when no scheme
file defines the key at all."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from configparser import ConfigParser, Error as ConfigError

from PySide6.QtCore import QStandardPaths
from PySide6.QtGui import QColor, QPalette

SET = "Colors:View"      # the KColorScheme set a plot area belongs to
BREEZE = {"ForegroundPositive": QColor(39, 174, 96),
          "ForegroundNegative": QColor(218, 68, 83),
          "ForegroundNeutral": QColor(246, 116, 0),
          "DecorationFocus": QColor(61, 174, 233),
          "DecorationHover": QColor(61, 174, 233)}
KEYS = tuple(BREEZE) + ("ForegroundInactive",)


@dataclass(frozen=True)
class Colors:
    """One colour per meaning, never per hue."""
    positive: QColor      # a healthy live reading
    negative: QColor      # a hard limit or a failure
    neutral: QColor       # a warning
    focus: QColor         # the object being edited
    hover: QColor         # the handle under the pointer
    inactive: QColor      # guides, hints, secondary text


def _triple(raw: str | None) -> QColor | None:
    """`ForegroundPositive=39,174,96` to a QColor. Reject anything that is not
    three components in 0..255: a corrupt scheme file must not paint an invalid
    colour."""
    if raw is None:
        return None
    try:
        rgb = tuple(int(x.strip()) for x in raw.split(","))
    except ValueError:
        return None
    if len(rgb) != 3 or not all(0 <= v <= 255 for v in rgb):
        return None
    return QColor(*rgb)


@lru_cache(maxsize=4)
def scheme(path: str) -> dict[str, QColor]:
    """Every KEYS colour the scheme file at `path` actually defines. Empty for
    an empty path, which is the non-KDE session with no scheme file at all."""
    if not path:
        return {}
    # Not QSettings: it keeps a process-wide cache of parsed ini files keyed on
    # the file's timestamp and size, so a colour-scheme switch that rewrites
    # kdeglobals to the same size within the same second is invisible to it,
    # sync() included. configparser reads the file every time, which is what a
    # cache this module clears itself (see forget) needs.
    cfg = ConfigParser(strict=False, interpolation=None)
    cfg.optionxform = str            # KDE keys are case-sensitive
    try:
        cfg.read(path, encoding="utf-8")
    except (OSError, UnicodeDecodeError, ConfigError):
        return {}                    # unreadable or not an ini: use the fallbacks
    if not cfg.has_section(SET):
        return {}
    got = ((k, _triple(cfg.get(SET, k, fallback=None))) for k in KEYS)
    return {k: c for k, c in got if c is not None}


def _luminance(c: QColor) -> float:
    """WCAG 2.1 relative luminance: linearize each sRGB component, then weight."""
    def lin(v: float) -> float:
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return (0.2126 * lin(c.redF()) + 0.7152 * lin(c.greenF())
            + 0.0722 * lin(c.blueF()))


def contrast(a: QColor, b: QColor) -> float:
    """WCAG contrast ratio, 1.0 (identical) to 21.0 (black on white)."""
    la, lb = sorted((_luminance(a), _luminance(b)))
    return (lb + 0.05) / (la + 0.05)


# Not WCAG 1.4.3 (4.5 for normal text), deliberately: BreezeLight's own
# ForegroundInactive scores 3.69 against its window and 4.21 against its View
# background, so a 4.5 floor would reject the default KDE light scheme and paint
# something the rest of the desktop does not use. 3.0 is 1.4.11's floor for a UI
# component: high enough to catch an illegible pairing, low enough to keep every
# scheme KDE itself ships. Raising it means overriding the user's choice.
MIN_CONTRAST = 3.0
# BreezeLight and BreezeDark ForegroundInactive. Unlike the accents these two
# differ, so the polarity of the window decides which one applies.
INACTIVE_ON_LIGHT = QColor(112, 125, 138)
INACTIVE_ON_DARK = QColor(161, 169, 177)


def kdeglobals() -> str:
    return QStandardPaths.locate(
        QStandardPaths.StandardLocation.GenericConfigLocation, "kdeglobals")


def forget() -> None:
    """Drop the memoized read. A frontend calls this when the scheme changes
    under it, from two triggers, because one alone is not enough: the palette
    event alone can arrive before the scheme applier has finished rewriting
    kdeglobals, and the file watch alone misses a palette change that writes no
    file. Both land on this one call, so a double trigger only costs one reread."""
    scheme.cache_clear()


def _inactive(palette: QPalette, from_scheme: QColor | None) -> QColor:
    """The first candidate that clears MIN_CONTRAST against the window.

    Guides, hints and secondary labels are the one role with no safe constant,
    and two ways to become illegible. The scheme's own value is wrong when the
    caller overrode the palette (--dark against a light scheme), and the
    style's disabled text is a background tint, not a text colour: Fusion's
    #bebebe on its own #efefef window is a ratio of 1.6."""
    window = palette.color(QPalette.ColorRole.Window)
    breeze = (INACTIVE_ON_DARK if _luminance(window) < 0.18
              else INACTIVE_ON_LIGHT)
    for cand in (from_scheme,
                 palette.color(QPalette.ColorGroup.Disabled,
                               QPalette.ColorRole.WindowText),
                 breeze):
        if cand is not None and contrast(cand, window) >= MIN_CONTRAST:
            return cand
    # Nothing dimmed is legible on this window, so stop dimming.
    return palette.color(QPalette.ColorRole.WindowText)


def colors(palette: QPalette, path: str | None = None) -> Colors:
    """Resolve every semantic colour the widgets need. `path` overrides the
    scheme file lookup; pass "" to force the fallbacks."""
    s = scheme(kdeglobals() if path is None else path)
    return Colors(
        positive=s.get("ForegroundPositive", BREEZE["ForegroundPositive"]),
        negative=s.get("ForegroundNegative", BREEZE["ForegroundNegative"]),
        neutral=s.get("ForegroundNeutral", BREEZE["ForegroundNeutral"]),
        focus=s.get("DecorationFocus", BREEZE["DecorationFocus"]),
        hover=s.get("DecorationHover", BREEZE["DecorationHover"]),
        inactive=_inactive(palette, s.get("ForegroundInactive")))
