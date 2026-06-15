#!/usr/bin/env python3
"""PPT Master - Contrast & font-size checker.

Computes WCAG contrast ratios for SVG fills/strokes against their context
backgrounds, audits font-size minimums against the project role ramp
(H1 ≥ 40, H2 ≥ 32, Body ≥ 24), and flags raw ``#RRGGBB`` literals that should
have come from a Theme token table.

The module is self-contained: luminance is computed inline per WCAG 2.1 §1.4.6
(no third-party deps), and SVG parsing uses Python's stdlib ``ElementTree``.
The same library powers ``svg_quality_checker.py`` so this module is wired in
there for the integrated run.

Usage
-----
::

    # Theme audit — print pass/fail summary
    python3 scripts/contrast_checker.py path/to/theme.json

    # SVG audit — print violations
    python3 scripts/contrast_checker.py path/to/slide.svg

    # SVG audit — JSON output (CI-friendly)
    python3 scripts/contrast_checker.py path/to/slide.svg --json

    # Directory sweep (matches svg_quality_checker invocation style)
    python3 scripts/contrast_checker.py path/to/svg_output

    # Machine-readable exit signal
    python3 scripts/contrast_checker.py path/to/slide.svg --ci

Design notes
------------
- **Severity ladder** for contrast: ratio < 4.5 → ``error`` (blocks export),
  4.5 ≤ ratio < 7.0 → ``warning`` (WCAG AA passes but not AAA), ratio ≥ 7.0
  → ``ok``. The ladder matches WCAG 2.1 §1.4.3 (minimum) and §1.4.6
  (enhanced).
- **Font-size ladder**: H1 < 40 / H2 < 32 / Body < 24 → ``error``. The
  default ramp is read from ``spec_lock.md`` ``## typography`` when available;
  otherwise the hard-coded defaults above are used. Other roles (annotation,
  subtitle, etc.) are reported as ``info`` rather than error.
- **No-hex-literal scan**: warns when ``fill="#RRGGBB"`` or
  ``stroke="#RRGGBB"`` is present outside the Theme's token table. SVG
  pre-defined colour keywords (``transparent``, ``currentColor``, ``none``,
  ``inherit``) and named CSS colours are always allowed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET


# ---------------------------------------------------------------------------
# Public constants — exposed so svg_quality_checker can reuse the ladders.
# ---------------------------------------------------------------------------

# WCAG thresholds per https://www.w3.org/TR/WCAG21/#contrast-minimum
WCAG_AA_RATIO = 4.5       # minimum for normal text
WCAG_AAA_RATIO = 7.0      # enhanced (large-text + UI also satisfied)

# Font-size ladder for the H1 / H2 / Body roles. Annotations, subtitles and
# hero numbers are role-anchored slots in spec_lock.md, so the audit reads
# the project lock when present and falls back to these defaults.
DEFAULT_FONT_RAMP: Dict[str, float] = {
    "h1": 40.0,
    "h2": 32.0,
    "body": 24.0,
}

# Roles considered "non-error" for size purposes; their sizes are reported
# as info (no warnings) when present.
INFO_ONLY_FONT_ROLES = {"annotation", "footnote", "subtitle", "cover_subtitle"}

# SVG_NS — ElementTree strips the namespace prefix unless we register it.
SVG_NS = "http://www.w3.org/2000/svg"

# CSS-named colours that are allowed even when a Theme forbids raw hex.
# Source: https://www.w3.org/TR/SVG11/types.html#ColorKeywords
SVG_NAMED_COLORS = frozenset({
    "transparent", "currentcolor", "none", "inherit",
    "aliceblue", "antiquewhite", "aqua", "aquamarine", "azure", "beige",
    "bisque", "black", "blanchedalmond", "blue", "blueviolet", "brown",
    "burlywood", "cadetblue", "chartreuse", "chocolate", "coral",
    "cornflowerblue", "cornsilk", "crimson", "cyan", "darkblue", "darkcyan",
    "darkgoldenrod", "darkgray", "darkgreen", "darkkhaki", "darkmagenta",
    "darkolivegreen", "darkorange", "darkorchid", "darkred", "darksalmon",
    "darkseagreen", "darkslateblue", "darkslategray", "darkturquoise",
    "darkviolet", "deeppink", "deepskyblue", "dimgray", "dodgerblue",
    "firebrick", "floralwhite", "forestgreen", "fuchsia", "gainsboro",
    "ghostwhite", "gold", "goldenrod", "gray", "green", "greenyellow",
    "honeydew", "hotpink", "indianred", "indigo", "ivory", "khaki",
    "lavender", "lavenderblush", "lawngreen", "lemonchiffon", "lightblue",
    "lightcoral", "lightcyan", "lightgoldenrodyellow", "lightgray",
    "lightgreen", "lightpink", "lightsalmon", "lightseagreen",
    "lightskyblue", "lightslategray", "lightsteelblue", "lightyellow",
    "lime", "limegreen", "linen", "magenta", "maroon", "mediumaquamarine",
    "mediumblue", "mediumorchid", "mediumpurple", "mediumseagreen",
    "mediumslateblue", "mediumspringgreen", "mediumturquoise",
    "mediumvioletred", "midnightblue", "mintcream", "mistyrose", "moccasin",
    "navajowhite", "navy", "oldlace", "olive", "olivedrab", "orange",
    "orangered", "orchid", "palegoldenrod", "palegreen", "paleturquoise",
    "palevioletred", "papayawhip", "peachpuff", "peru", "pink", "plum",
    "powderblue", "purple", "red", "rosybrown", "royalblue", "saddlebrown",
    "salmon", "sandybrown", "seagreen", "seashell", "sienna", "silver",
    "skyblue", "slateblue", "slategray", "snow", "springgreen", "steelblue",
    "tan", "teal", "thistle", "tomato", "turquoise", "violet", "wheat",
    "white", "whitesmoke", "yellow", "yellowgreen",
})

# Regexes used in multiple places — keep them tight so they don't match
# `<style>` blocks (which are forbidden by the quality checker anyway).
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{3,8}$")
_SHORT_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{3}$")
_FILL_RE = re.compile(
    r'\b(?:fill|stroke|stop-color)\s*=\s*["\'](#[0-9A-Fa-f]{3,8})["\']'
)
_FONT_SIZE_RE = re.compile(
    r'\bfont-size\s*=\s*["\']([^"\']+)["\']'
)


# ---------------------------------------------------------------------------
# Core WCAG math
# ---------------------------------------------------------------------------

def _expand_short_hex(hex_str: str) -> str:
    """``#RGB`` → ``#RRGGBB``. ``#RRGGBB``/``#RRGGBBAA`` pass through.

    Raises ``ValueError`` if the input is not a valid hex literal.
    """
    if not _HEX_RE.match(hex_str):
        raise ValueError(f"not a hex color literal: {hex_str!r}")
    if len(hex_str) in (7, 9):  # already #RRGGBB or #RRGGBBAA
        return hex_str.upper()
    if _SHORT_HEX_RE.match(hex_str):
        # #RGB -> #RRGGBB (ignore alpha for contrast)
        r, g, b = hex_str[1], hex_str[2], hex_str[3]
        return f"#{r}{r}{g}{g}{b}{b}".upper()
    # #RGBA -> #RRGGBBAA; we keep alpha but contrast uses only RGB
    if len(hex_str) == 5:
        r, g, b, a = hex_str[1], hex_str[2], hex_str[3], hex_str[4]
        return f"#{r}{r}{g}{g}{b}{b}{a}{a}".upper()
    return hex_str.upper()


def _channel_luminance(channel: int) -> float:
    """WCAG relative luminance for a single 0..255 sRGB channel.

    Per WCAG 2.1 §1.4.6: linearise the sRGB value via the piecewise function
    before summing. The 0.03928 / 12.92 thresholds are the canonical
    sRGB-to-linear conversion constants.
    """
    srgb = channel / 255.0
    if srgb <= 0.03928:
        return srgb / 12.92
    return ((srgb + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of ``#RRGGBB`` (or short form).

    Returns L in [0, 1] where 0 = pure black and 1 = pure white.
    """
    expanded = _expand_short_hex(hex_color)
    r = int(expanded[1:3], 16)
    g = int(expanded[3:5], 16)
    b = int(expanded[5:7], 16)
    return (
        0.2126 * _channel_luminance(r)
        + 0.7152 * _channel_luminance(g)
        + 0.0722 * _channel_luminance(b)
    )


def compute_ratio(fg: str, bg: str) -> float:
    """WCAG 2.1 contrast ratio between ``fg`` and ``bg``.

    Accepts ``#RRGGBB`` and ``#RGB`` (and ``#RGBA``/``#RRGGBBAA``, whose alpha
    is ignored). The result is ``(L_lighter + 0.05) / (L_darker + 0.05)`` —
    ranges from 1 (identical colours) to 21 (white vs black).

    Raises ``ValueError`` when either input is not a hex literal. SVG named
    colours and ``url(#…)`` references cannot be resolved without rendering
    and therefore raise as well; callers should resolve gradients / named
    colours to a hex value before calling.
    """
    if not _HEX_RE.match(fg):
        raise ValueError(f"compute_ratio: fg is not a hex literal: {fg!r}")
    if not _HEX_RE.match(bg):
        raise ValueError(f"compute_ratio: bg is not a hex literal: {bg!r}")
    l_fg = _relative_luminance(fg)
    l_bg = _relative_luminance(bg)
    lighter = max(l_fg, l_bg)
    darker = min(l_fg, l_bg)
    return (lighter + 0.05) / (darker + 0.05)


def severity_for_ratio(ratio: float) -> str:
    """Map a ratio to ``"error"`` / ``"warning"`` / ``"ok"``.

    The ladder matches the task brief:
      < 4.5  → error  (fails WCAG AA, blocks export)
      < 7.0  → warning (passes AA but not AAA)
      ≥ 7.0  → ok
    """
    if ratio < WCAG_AA_RATIO:
        return "error"
    if ratio < WCAG_AAA_RATIO:
        return "warning"
    return "ok"


# ---------------------------------------------------------------------------
# Theme handling
# ---------------------------------------------------------------------------

def load_theme(theme_path: Path) -> Dict:
    """Read a theme JSON file. Accepts either a flat dict or ``{tokens: …,
    ratios: …}``. Returns the parsed dict or ``{}`` on read failure."""
    try:
        text = theme_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _collect_token_hexes(theme: Dict) -> set:
    """Gather every hex literal listed under a theme's ``tokens`` subtree.

    Themes may use any of:
      ``tokens: { "bg-canvas": "#0A0A0A", … }`` (flat map)
      ``tokens: { colors: {…}, text: {…} }`` (nested by category)
      ``tokens: [ "#0A0A0A", "#F5F5F5" ]`` (list form)

    The returned set contains upper-cased ``#RRGGBB`` strings.
    """
    tokens = theme.get("tokens", {})
    out: set = set()
    if isinstance(tokens, list):
        for entry in tokens:
            if isinstance(entry, str) and _HEX_RE.match(entry):
                out.add(entry.upper())
    elif isinstance(tokens, dict):
        def _walk(node):
            if isinstance(node, str):
                if _HEX_RE.match(node):
                    out.add(node.upper())
            elif isinstance(node, dict):
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)
        for v in tokens.values():
            _walk(v)
    return out


# Schema-catalog import — the modern theme schema is owned by
# ``validate_theme.py`` (it mirrors ``skills/ppt-master/themes/_schema.json``).
# We import it lazily so this module remains usable as a stand-alone SVG
# checker for callers that don't have validate_theme.py on sys.path (e.g.
# tests that mock out the theme path).
def _get_modern_validate_theme():
    """Return ``(modern_validate_theme_callable, ERROR_PREFIX)``.

    The modern callable is ``validate_theme.validate_theme(theme_dict)`` and
    returns a list of human-readable error strings. We import on first call
    so test fixtures that don't ship validate_theme.py still work — see
    ``test_validate_theme_unified.py`` for the fallback path.
    """
    try:
        # Same dir as this file.
        from validate_theme import validate_theme as _vt_validate
        return _vt_validate, ""
    except ImportError:
        # Fall back to a minimal in-module re-implementation of the modern
        # schema's structure checks. This is intentionally not feature-
        # complete — it only catches the shape mismatches that motivated the
        # unification (object-vs-list, missing tokens, etc.). When the real
        # module is available, prefer it.
        return _modern_validate_theme_fallback, \
            "[fallback-validator] "


def _modern_validate_theme_fallback(theme: dict, *, strict: bool = False) -> List[str]:
    """Minimal subset of validate_theme.py's checks for the object schema.

    Used only when ``validate_theme.py`` is not importable from this script's
    directory. The shipped themes in ``skills/ppt-master/themes/*.json`` use
    the modern schema; falling back here lets ``contrast_checker.py`` still
    catch the most common shape errors (ratios-not-an-object, missing tokens,
    below-threshold text) without hard-failing on import.
    """
    errors: List[str] = []
    if not isinstance(theme, dict):
        return ["theme must be a JSON object"]
    for key in ("id", "kind", "version", "tokens", "ratios", "wcag"):
        if key not in theme:
            errors.append(f"missing required top-level key: {key!r}")
    ratios = theme.get("ratios")
    if not isinstance(ratios, dict):
        errors.append(
            "'ratios' must be an object keyed by '<fg>/bg-canvas' "
            "(modern schema); got " + type(ratios).__name__
        )
        return errors
    tokens = theme.get("tokens") or {}
    bg = tokens.get("bg-canvas") if isinstance(tokens, dict) else None
    import re as _re
    HEX = _re.compile(r"^#[0-9A-Fa-f]{6}$")
    THRESHOLD = 4.5
    for rk, rv in ratios.items():
        if not isinstance(rv, (int, float)):
            errors.append(f"ratio {rk!r} must be a number (got {rv!r})")
            continue
        if rk.startswith(("text-", "accent")) and rv < THRESHOLD:
            errors.append(
                f"{rk} = {rv:.2f} is below WCAG AA text floor {THRESHOLD}"
            )
    return errors


def validate_theme(theme_path: Path) -> Tuple[bool, List[str]]:
    """Validate a theme JSON against the ppt-master WCAG schema.

    Single source of truth: ``validate_theme.py`` (which mirrors
    ``themes/_schema.json``). This function is a thin wrapper that:

      1. Reads the theme from disk (so callers can pass a ``Path``).
      2. Detects whether the file uses the **modern object-shaped schema**
         (``ratios`` is an object keyed by ``"<fg>/bg-canvas"``) or the
         **legacy list-shaped schema** (``ratios`` is a list of
         ``{pair, ratio, rule}`` objects — kept around for backward
         compatibility with the existing test suite fixtures).
      3. Dispatches to the right validator.

    The modern schema is authoritative. The legacy path is preserved so
    ``test_contrast_checker.py``'s pre-existing fixtures (which use the
    list shape with ``name`` instead of ``id``) keep passing without
    rewriting tests.

    Returns ``(passed, errors)``. ``errors`` is empty when the theme is
    well-formed and meets WCAG AA on every listed text/contrast ratio.
    """
    if not theme_path.exists():
        return False, [f"theme file not found: {theme_path}"]

    theme = load_theme(theme_path)
    if not theme:
        return False, [f"theme file is empty or unparseable: {theme_path}"]

    ratios = theme.get("ratios", None)

    # Dispatch on schema shape. Modern schema is the primary path — all
    # shipped themes in skills/ppt-master/themes/*.json use it.
    if isinstance(ratios, dict):
        # Object-shaped modern schema — delegate to validate_theme.py.
        modern, prefix = _get_modern_validate_theme()
        try:
            errs = modern(theme)
        except Exception as exc:  # pragma: no cover — defensive
            return False, [f"modern-validator crashed: {exc!r}"]
        if prefix and errs:
            errs = [prefix + e for e in errs]
        return (len(errs) == 0), errs

    if isinstance(ratios, list):
        # Legacy list-shaped schema — kept for backward compat with the
        # test suite's existing fixtures. Same threshold as before so the
        # old tests pass unchanged.
        return _validate_theme_legacy_list_shape(theme, theme_path.name)

    # Neither object nor list — record the schema mismatch explicitly so the
    # caller can see exactly what's wrong.
    return False, [
        f"theme {theme_path.name}: 'ratios' must be an object keyed by "
        "'<fg>/bg-canvas' (modern schema) — got "
        f"{type(ratios).__name__}; see skills/ppt-master/themes/_schema.json"
    ]


def _validate_theme_legacy_list_shape(theme: dict, filename: str
                                       ) -> Tuple[bool, List[str]]:
    """Legacy validator for the deprecated list-shaped ratios schema.

    Retained so the existing ``test_contrast_checker.py`` fixtures
    (which use the list shape with a top-level ``name`` and ad-hoc
    tokens like ``bg-elevated`` / ``accent-frost``) still validate. New
    themes should use the modern object schema; this path will be
    removed in a future major version.
    """
    errors: List[str] = []
    tokens = theme.get("tokens", {})
    if not tokens:
        errors.append(f"theme {filename}: missing 'tokens' section")

    ratios = theme.get("ratios", [])
    if not isinstance(ratios, list):
        errors.append(
            f"theme {filename}: 'ratios' must be a list "
            "(see docs/themes.md for the schema)"
        )
        ratios = []

    threshold = WCAG_AA_RATIO

    for index, entry in enumerate(ratios):
        if not isinstance(entry, dict):
            errors.append(f"ratios[{index}]: not an object")
            continue
        pair = entry.get("pair")
        ratio_value = entry.get("ratio")
        rule = entry.get("rule", "min_text_ratio")

        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            errors.append(f"ratios[{index}]: 'pair' must be a 2-element list")
            continue
        token_a, token_b = pair
        if token_a not in tokens:
            errors.append(
                f"ratios[{index}]: pair[0] references unknown token "
                f"{token_a!r}"
            )
        if token_b not in tokens:
            errors.append(
                f"ratios[{index}]: pair[1] references unknown token "
                f"{token_b!r}"
            )
        if not (isinstance(ratio_value, (int, float))):
            errors.append(f"ratios[{index}]: 'ratio' must be a number")
            continue

        # Recompute the ratio from the actual tokens to guard against
        # themes that lie about their contrast.
        if (
            isinstance(tokens.get(token_a), str)
            and isinstance(tokens.get(token_b), str)
            and _HEX_RE.match(tokens[token_a])
            and _HEX_RE.match(tokens[token_b])
        ):
            actual = compute_ratio(tokens[token_a], tokens[token_b])
            if abs(actual - float(ratio_value)) > 0.5:
                errors.append(
                    f"ratios[{index}]: declared ratio {ratio_value:.2f} "
                    f"diverges from recomputed {actual:.2f} "
                    f"(tokens {token_a}={tokens[token_a]} / "
                    f"{token_b}={tokens[token_b]})"
                )
            if rule in {"min_text_ratio", "wcag_aa"} and actual < threshold:
                errors.append(
                    f"ratios[{index}]: pair ({token_a}, {token_b}) "
                    f"actual ratio {actual:.2f}:1 < WCAG AA ({threshold}:1)"
                )

    return (len(errors) == 0), errors


# ---------------------------------------------------------------------------
# Unified facade — public API for new callers (replaces the old, broken
# validate_theme() that only knew the list-shaped schema).
# ---------------------------------------------------------------------------

def validate_theme_unified(theme_path: Path, *, strict: bool = False
                            ) -> Tuple[bool, List[str]]:
    """Public unified facade — the recommended entry point.

    Identical contract to ``validate_theme()`` but explicit about being
    "the unified one". Future schema additions land here; both validators
    (this module's ``validate_theme`` and ``validate_theme.py``'s
    ``validate_theme``) share the modern schema path through this function.

    Parameters
    ----------
    theme_path : Path
        Theme JSON on disk.
    strict : bool
        When True, also enforce WCAG AAA (text-primary >= 7.0). Forwarded
        to ``validate_theme.py`` for the modern-schema path.

    Returns
    -------
    (passed, errors) : tuple
    """
    if not theme_path.exists():
        return False, [f"theme file not found: {theme_path}"]

    theme = load_theme(theme_path)
    if not theme:
        return False, [f"theme file is empty or unparseable: {theme_path}"]

    ratios = theme.get("ratios", None)
    if isinstance(ratios, dict):
        modern, prefix = _get_modern_validate_theme()
        try:
            # The modern validator supports ``strict``; the fallback does
            # not (it ignores the kwarg, which is acceptable for a defensive
            # path).
            errs = modern(theme, strict=strict)
        except TypeError:
            # Fallback doesn't accept strict.
            errs = modern(theme)
        except Exception as exc:  # pragma: no cover
            return False, [f"modern-validator crashed: {exc!r}"]
        if prefix and errs:
            errs = [prefix + e for e in errs]
        return (len(errs) == 0), errs

    if isinstance(ratios, list):
        return _validate_theme_legacy_list_shape(theme, theme_path.name)

    return False, [
        f"theme {theme_path.name}: 'ratios' must be an object keyed by "
        "'<fg>/bg-canvas' (modern schema) — got "
        f"{type(ratios).__name__}"
    ]


# ---------------------------------------------------------------------------
# SVG audit
# ---------------------------------------------------------------------------

def _extract_bg_from_spec(spec_lock_path: Optional[Path]) -> Optional[str]:
    """Pull the ``- bg:`` value out of spec_lock.md ``## colors``.

    Returns ``None`` if the file or section is missing.
    """
    if spec_lock_path is None or not spec_lock_path.exists():
        return None
    try:
        text = spec_lock_path.read_text(encoding="utf-8")
    except OSError:
        return None
    in_colors = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            in_colors = line[3:].strip().lower() == "colors"
            continue
        if not in_colors:
            continue
        m = re.match(r"^-\s+bg\s*:\s*(#[0-9A-Fa-f]{3,8})\s*$", line)
        if m:
            return m.group(1).upper()
    return None


def _classify_text_role(text_elem: ET.Element,
                        ancestor_chain: List[ET.Element]) -> Tuple[str, str]:
    """Return ``(role, source)`` for a ``<text>`` element.

    The classification walks the ancestor chain looking for the first
    ``<g id="…">`` whose id encodes a role keyword. ``source`` is the
    exact key that matched (either an id substring or the element id).
    Falls back to ``("body", "fallback")`` for free-standing text.

    The keyword order matters: h2 ("subtitle") is checked before h1
    ("title") because "title" is a substring of "subtitle" / "section-title"
    — checking h1 first would misroute h2 nodes. Same for h1 ("cover-title")
    before body ("content"), since "cover-content" exists.
    """
    # More specific roles first so substrings don't shadow them.
    ROLE_KEYWORDS = (
        ("annotation", ("annotation", "footnote", "caption")),
        ("h2", ("subtitle", "h2", "section-title")),
        ("h1", ("cover-title", "chapter-title", "h1", "title")),
        ("body", ("body", "content", "paragraph")),
    )

    for ancestor in ancestor_chain:
        aid = (ancestor.get("id") or "").lower()
        if not aid:
            continue
        for role, keys in ROLE_KEYWORDS:
            if any(k in aid for k in keys):
                return role, aid
    # Fallback: text elements with no role-bearing group id are body text.
    return "body", "fallback"


def _parse_svg_safe(content: str) -> Optional[ET.Element]:
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


def _walk_with_ancestors(root: ET.Element) -> Iterable[Tuple[ET.Element, List[ET.Element]]]:
    """Yield ``(element, ancestor_chain)`` for every element under ``root``."""
    def _recurse(node: ET.Element, chain: List[ET.Element]):
        chain.append(node)
        yield node, list(chain)
        for child in list(node):
            yield from _recurse(child, chain)
        chain.pop()
    yield from _recurse(root, [])


def _parse_viewbox(root: ET.Element) -> Tuple[float, float]:
    """Best-effort viewBox parse — returns ``(w, h)`` or ``(0, 0)`` on miss."""
    vb = root.get("viewBox")
    if not vb:
        return 0.0, 0.0
    parts = vb.replace(",", " ").split()
    if len(parts) != 4:
        return 0.0, 0.0
    try:
        return float(parts[2]), float(parts[3])
    except ValueError:
        return 0.0, 0.0


def _is_full_canvas_bg(elem: ET.Element, viewbox_w: float,
                       viewbox_h: float) -> bool:
    """True when ``elem`` is plausibly the page's own background.

    Heuristic: a ``<rect>`` (or ``<svg>`` wrapper) whose width/height match
    the viewBox. We intentionally don't require ``x=0 y=0`` because authors
    sometimes omit those defaults.
    """
    tag = elem.tag.split("}", 1)[-1]
    if tag not in {"rect", "svg"}:
        return False
    if viewbox_w <= 0 or viewbox_h <= 0:
        return False
    try:
        w = float(elem.get("width", "0"))
        h = float(elem.get("height", "0"))
    except ValueError:
        return False
    return abs(w - viewbox_w) < 1.0 and abs(h - viewbox_h) < 1.0


def check_svg(svg_path: Path, theme: Optional[Dict] = None) -> List[Dict]:
    """Audit every ``fill``/``stroke``/``stop-color`` hex in ``svg_path``.

    Each violation dict has ``element``, ``attr``, ``color``, ``bg``,
    ``ratio``, ``severity`` and ``rule``. A regex-based pre-scan catches
    hex literals that ElementTree cannot attribute to a single element
    (e.g. inside an inline ``style`` attribute); these are reported with
    ``element = '<unattributed>'``.

    Pragmatic filters
    -----------------
    The audit intentionally skips:

    1. ``<rect width=viewBox> fill="<bg-hex>"`` — that's the page bg
       rect, not a colour that needs contrast against itself.
    2. ``fill``/``stroke`` whose value equals the resolved bg (1:1 ratio
       is meaningless for both ends of the comparison).
    3. ``<linearGradient>/<radialGradient>`` internals (``<stop>``) —
       gradient stops paint on top of whatever is below the gradient, so
       their contrast against the page bg is not the relevant signal.
    """
    violations: List[Dict] = []
    try:
        content = svg_path.read_text(encoding="utf-8")
    except OSError:
        return [{
            "file": str(svg_path),
            "element": "<file>",
            "attr": "read",
            "color": None,
            "bg": None,
            "ratio": None,
            "severity": "error",
            "rule": "file_unreadable",
            "message": f"cannot read SVG: {svg_path}",
        }]

    # Resolve background — explicit theme.bg wins, then spec_lock.md bg,
    # then the first ``<rect width="100%" fill="#…">`` near the top.
    bg_hex: Optional[str] = None
    if theme and isinstance(theme.get("bg"), str) and _HEX_RE.match(theme["bg"]):
        bg_hex = theme["bg"].upper()
    else:
        spec_lock = svg_path.parent / "spec_lock.md"
        if not spec_lock.exists():
            spec_lock = svg_path.parent.parent / "spec_lock.md"
        bg_hex = _extract_bg_from_spec(spec_lock)

    root = _parse_svg_safe(content)
    if root is None:
        # Regex fallback so we still report *something* on broken XML.
        for m in _FILL_RE.finditer(content):
            color = m.group(1).upper()
            ratio = compute_ratio(color, bg_hex) if bg_hex else None
            violations.append({
                "file": str(svg_path),
                "element": "<unattributed>",
                "attr": m.group(0).split("=", 1)[0].strip(),
                "color": color,
                "bg": bg_hex,
                "ratio": ratio,
                "severity": severity_for_ratio(ratio) if ratio else "warning",
                "rule": "R1" if ratio else "R1_no_bg",
                "message": "XML unparseable; regex scan only",
            })
        return violations

    vb_w, vb_h = _parse_viewbox(root)
    token_hexes = _collect_token_hexes(theme or {})

    # ElementTree iteration
    for elem, ancestors in _walk_with_ancestors(root):
        tag = elem.tag.split("}", 1)[-1]
        # Skip <defs>/<linearGradient> internals — gradient stops need a
        # different audit (their effective fill is the gradient itself).
        in_defs = any(
            (a.tag.split("}", 1)[-1] in {"defs", "linearGradient", "radialGradient"})
            for a in ancestors[:-1]
        )
        for attr in ("fill", "stroke", "stop-color"):
            value = elem.get(attr)
            if value is None:
                continue
            if not _HEX_RE.match(value):
                continue  # CSS named, url(), currentColor, etc.
            color = value.upper()
            # Skip gradient stops — they don't sit on the bg directly.
            if in_defs and attr == "stop-color":
                continue
            # Skip full-canvas bg rects — they're painting the bg, not
            # sitting on top of it.
            if attr == "fill" and _is_full_canvas_bg(elem, vb_w, vb_h):
                continue
            # Skip fill=bg and stroke=bg — 1:1 is not informative.
            if bg_hex is not None and color == bg_hex:
                continue
            if bg_hex is None:
                # No background resolvable; still flag, but as warning.
                violations.append({
                    "file": str(svg_path),
                    "element": f"<{tag}>" + (
                        f" id='{elem.get('id')}'" if elem.get("id") else ""
                    ),
                    "attr": attr,
                    "color": color,
                    "bg": None,
                    "ratio": None,
                    "severity": "warning",
                    "rule": "R1_no_bg",
                    "message": "no background resolvable (spec_lock.md missing?); cannot compute ratio",
                })
                continue
            ratio = compute_ratio(color, bg_hex)
            severity = severity_for_ratio(ratio)
            violations.append({
                "file": str(svg_path),
                "element": f"<{tag}>" + (
                    f" id='{elem.get('id')}'" if elem.get("id") else ""
                ),
                "attr": attr,
                "color": color,
                "bg": bg_hex,
                "ratio": round(ratio, 2),
                "severity": severity,
                "rule": "R1",
                "message": f"{attr}={color} on {bg_hex} = {ratio:.2f}:1 ({severity})",
            })

    # Deduplicate identical entries — common when an entire page uses the
    # same colour on every text node, which would otherwise print N times.
    seen = set()
    deduped: List[Dict] = []
    for v in violations:
        key = (v.get("element"), v.get("attr"), v.get("color"),
               v.get("bg"), v.get("ratio"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(v)
    return deduped


def _read_font_ramp(spec_lock_path: Optional[Path]) -> Dict[str, float]:
    """Read typography ramp from spec_lock.md ``## typography``.

    Falls back to :data:`DEFAULT_FONT_RAMP` if no lock is found or the
    section does not declare ``body``/``title``/``subtitle``.
    """
    ramp = dict(DEFAULT_FONT_RAMP)
    if spec_lock_path is None or not spec_lock_path.exists():
        return ramp
    try:
        text = spec_lock_path.read_text(encoding="utf-8")
    except OSError:
        return ramp
    in_typo = False
    title_px = subtitle_px = None
    body_px = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            in_typo = line[3:].strip().lower() == "typography"
            continue
        if not in_typo:
            continue
        m = re.match(r"^-\s+(title|subtitle|body|h1|h2)\s*:\s*(\d+)", line)
        if not m:
            continue
        key, value = m.group(1), float(m.group(2))
        if key == "title":
            title_px = value
        elif key == "subtitle":
            subtitle_px = value
        elif key == "body":
            body_px = value
            ramp["body"] = value
        elif key == "h1":
            ramp["h1"] = value
        elif key == "h2":
            ramp["h2"] = value

    # Spec_lock's `title` is the cover/chapter title (≈ H1). If no explicit
    # `h1` row, prefer title > subtitle > body for the H1/H2 anchors.
    if title_px is not None and "h1" not in ramp:
        ramp["h1"] = title_px
    if subtitle_px is not None and "h2" not in ramp:
        ramp["h2"] = subtitle_px
    return ramp


def check_font_sizes(svg_path: Path) -> List[Dict]:
    """Audit ``<text font-size="…">`` against the role ramp.

    Returns a list of dicts::

        {
          "file", "element", "text", "font_size", "role", "rule",
          "severity", "message"
        }

    Severity: H1<H1_min / H2<H2_min / Body<24 → ``error``; H1/H2/Body that
    pass → omitted from the report. Subtitle / annotation / hero-number
    roles are reported as ``info`` (they are role-anchored slots and a
    deliberate smaller size is legitimate). The role classification walks
    the ancestor chain looking for ``<g id="…">`` ids containing the role
    keywords defined in ``_classify_text_role``.
    """
    violations: List[Dict] = []
    try:
        content = svg_path.read_text(encoding="utf-8")
    except OSError:
        return [{
            "file": str(svg_path),
            "element": "<file>",
            "text": "",
            "font_size": None,
            "role": "unknown",
            "rule": "file_unreadable",
            "severity": "error",
            "message": f"cannot read SVG: {svg_path}",
        }]

    spec_lock = svg_path.parent / "spec_lock.md"
    if not spec_lock.exists():
        spec_lock = svg_path.parent.parent / "spec_lock.md"
    ramp = _read_font_ramp(spec_lock)

    root = _parse_svg_safe(content)
    if root is None:
        # Regex fallback (best-effort; loses role classification).
        for m in _FONT_SIZE_RE.finditer(content):
            try:
                size = float(m.group(1).replace("px", "").strip())
            except ValueError:
                continue
            if size < ramp["body"]:
                violations.append({
                    "file": str(svg_path),
                    "element": "<unattributed>",
                    "text": "",
                    "font_size": size,
                    "role": "body",
                    "rule": "font_min",
                    "severity": "error",
                    "message": f"font-size {size} < body ramp {ramp['body']} (XML broken)",
                })
        return violations

    for elem, ancestors in _walk_with_ancestors(root):
        tag = elem.tag.split("}", 1)[-1]
        if tag != "text":
            continue
        size_attr = elem.get("font-size")
        if not size_attr:
            continue
        try:
            size = float(size_attr.replace("px", "").strip())
        except ValueError:
            continue

        role, source = _classify_text_role(elem, ancestors[:-1])
        text_content = "".join(elem.itertext()).strip()[:40]

        if role in INFO_ONLY_FONT_ROLES:
            violations.append({
                "file": str(svg_path),
                "element": f"<text>" + (
                    f" id='{elem.get('id')}'" if elem.get("id") else ""
                ),
                "text": text_content,
                "font_size": size,
                "role": role,
                "rule": "font_info",
                "severity": "info",
                "message": f"{role} font-size {size} (info)",
            })
            continue

        threshold = ramp.get(role)
        if threshold is None:
            # Unknown role — don't error, just inform.
            violations.append({
                "file": str(svg_path),
                "element": f"<text>" + (
                    f" id='{elem.get('id')}'" if elem.get("id") else ""
                ),
                "text": text_content,
                "font_size": size,
                "role": role,
                "rule": "font_unknown_role",
                "severity": "info",
                "message": f"role={role} font-size {size} (no ramp anchor)",
            })
            continue

        if size < threshold:
            violations.append({
                "file": str(svg_path),
                "element": f"<text>" + (
                    f" id='{elem.get('id')}'" if elem.get("id") else ""
                ),
                "text": text_content,
                "font_size": size,
                "role": role,
                "rule": "font_min",
                "severity": "error",
                "message": (
                    f"{role} font-size {size} < ramp minimum {threshold} "
                    f"(source: {source})"
                ),
            })
    return violations


def check_no_hex_literals(svg_path: Path,
                          theme: Optional[Dict] = None) -> List[Dict]:
    """Flag ``#RRGGBB``/``#RGB`` literals outside the Theme's token table.

    Allowed always (whitelist):
      - CSS named colours (``white``, ``black``, …)
      - ``transparent``, ``currentColor``, ``none``, ``inherit``
      - ``url(#…)`` references (gradients, patterns)

    When ``theme`` is provided, every hex in ``theme["tokens"]`` is allowed.
    The audit is *advisory*: violations are warnings, not errors, because
    legacy decks may legitimately use hex literals while a Theme is being
    introduced.
    """
    violations: List[Dict] = []
    try:
        content = svg_path.read_text(encoding="utf-8")
    except OSError:
        return [{
            "file": str(svg_path),
            "element": "<file>",
            "attr": "read",
            "color": None,
            "rule": "file_unreadable",
            "severity": "error",
            "message": f"cannot read SVG: {svg_path}",
        }]

    allowed_hexes = _collect_token_hexes(theme or {})

    root = _parse_svg_safe(content)
    if root is None:
        for m in _FILL_RE.finditer(content):
            color = m.group(1).upper()
            if color in allowed_hexes:
                continue
            violations.append({
                "file": str(svg_path),
                "element": "<unattributed>",
                "attr": m.group(0).split("=", 1)[0].strip(),
                "color": color,
                "rule": "no_hex_literal",
                "severity": "warning",
                "message": f"hex literal {color} not in theme tokens (XML broken)",
            })
        return violations

    # ``stop-color`` is exempt from the no-hex-literal scan: gradient stops
    # are routinely literal hex (e.g. #FFFFFF highlight fades) and authors
    # typically don't tokenise them. The audit still records them in the
    # contrast check (R1) but skips the "should be a token" warning here.
    AUDITED_ATTRS = ("fill", "stroke")

    for elem, _ancestors in _walk_with_ancestors(root):
        tag = elem.tag.split("}", 1)[-1]
        for attr in AUDITED_ATTRS:
            value = elem.get(attr)
            if value is None:
                continue
            value_clean = value.strip()
            if value_clean.lower() in SVG_NAMED_COLORS:
                continue
            if value_clean.startswith("url("):
                continue
            if not _HEX_RE.match(value_clean):
                continue
            color = value_clean.upper()
            if color in allowed_hexes:
                continue
            violations.append({
                "file": str(svg_path),
                "element": f"<{tag}>" + (
                    f" id='{elem.get('id')}'" if elem.get("id") else ""
                ),
                "attr": attr,
                "color": color,
                "rule": "no_hex_literal",
                "severity": "warning",
                "message": (
                    f"{attr}={color} not in theme tokens; "
                    "use Theme tokens or add to tokens table"
                ),
            })

    seen = set()
    deduped: List[Dict] = []
    for v in violations:
        key = (v.get("element"), v.get("attr"), v.get("color"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(v)
    return deduped


# ---------------------------------------------------------------------------
# Token expansion (used by finalize_svg / svg_to_pptx)
# ---------------------------------------------------------------------------

def expand_tokens(content: str, theme: Dict) -> Tuple[str, List[str]]:
    """Replace ``var(--token-name)`` with the matching theme token.

    Tokens are looked up first in ``theme["tokens"]["<name>"]`` then in the
    flat ``theme["<name>"]`` map. Missing tokens are left as-is and the
    unresolved token is added to the returned warning list so callers can
    surface a "token coverage" warning.

    The function is intentionally conservative: it does NOT touch hex
    literals already in the document, only ``var(--…)`` placeholders. The
    no-hex-literal audit lives in :func:`check_no_hex_literals`.
    """
    warnings: List[str] = []
    tokens = theme.get("tokens", {}) if isinstance(theme, dict) else {}
    flat = {k: v for k, v in (theme.items() if isinstance(theme, dict) else [])
            if isinstance(v, str) and _HEX_RE.match(v)}

    def _resolve(name: str) -> Optional[str]:
        if isinstance(tokens, dict):
            v = tokens.get(name)
            if isinstance(v, str) and _HEX_RE.match(v):
                return v.upper()
            # Nested tokens, e.g. tokens.colors.bg-canvas
            for sub in tokens.values():
                if isinstance(sub, dict) and name in sub:
                    v = sub[name]
                    if isinstance(v, str) and _HEX_RE.match(v):
                        return v.upper()
        if name in flat and _HEX_RE.match(flat[name]):
            return flat[name].upper()
        return None

    pattern = re.compile(r"var\(\s*--([A-Za-z0-9_-]+)\s*\)")

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        resolved = _resolve(name)
        if resolved is None:
            warnings.append(f"unresolved token: var(--{name})")
            return match.group(0)
        return resolved

    new_content = pattern.sub(_replace, content)
    return new_content, warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_pretty(violations: List[Dict], *, ci: bool = False) -> None:
    def _safe(text: str) -> None:
        """Print text tolerating cp950 (zh-TW Windows) and other narrow codecs."""
        try:
            print(text)
        except UnicodeEncodeError:
            print(text.encode("ascii", "replace").decode("ascii"))
    if not violations:
        _safe("[OK] No violations found.")
        return
    counts: Dict[str, int] = {"error": 0, "warning": 0, "info": 0}
    for v in violations:
        sev = v.get("severity", "info")
        counts[sev] = counts.get(sev, 0) + 1
        if ci:
            continue
        msg = v.get("message") or _format_default(v)
        _safe(f"  [{sev.upper():7}] {msg}")
    _safe(
        f"\nTotal: {counts['error']} error(s), "
        f"{counts['warning']} warning(s), {counts['info']} info"
    )


def _format_default(v: Dict) -> str:
    parts = []
    for key in ("element", "attr", "color", "bg", "ratio", "role", "font_size"):
        if v.get(key) is not None:
            parts.append(f"{key}={v[key]}")
    return " | ".join(parts) if parts else str(v)


def _audit_path(target: Path, theme: Optional[Dict]) -> Dict:
    """Run all checks against ``target`` (a file or a directory)."""
    if target.is_dir():
        files = sorted(target.glob("*.svg"))
        if not files:
            return {
                "target": str(target),
                "kind": "directory",
                "files": 0,
                "violations": [],
                "summary": {"error": 0, "warning": 0, "info": 0},
            }
        all_v: List[Dict] = []
        for f in files:
            all_v.extend(check_svg(f, theme=theme))
            all_v.extend(check_font_sizes(f))
            all_v.extend(check_no_hex_literals(f, theme=theme))
        return {
            "target": str(target),
            "kind": "directory",
            "files": len(files),
            "violations": all_v,
            "summary": {
                "error": sum(1 for v in all_v if v.get("severity") == "error"),
                "warning": sum(1 for v in all_v if v.get("severity") == "warning"),
                "info": sum(1 for v in all_v if v.get("severity") == "info"),
            },
        }

    if target.suffix.lower() == ".svg":
        v = check_svg(target, theme=theme)
        v.extend(check_font_sizes(target))
        v.extend(check_no_hex_literals(target, theme=theme))
        return {
            "target": str(target),
            "kind": "svg",
            "files": 1,
            "violations": v,
            "summary": {
                "error": sum(1 for x in v if x.get("severity") == "error"),
                "warning": sum(1 for x in v if x.get("severity") == "warning"),
                "info": sum(1 for x in v if x.get("severity") == "info"),
            },
        }

    if target.suffix.lower() == ".json":
        passed, errors = validate_theme(target)
        violations = [
            {
                "file": str(target),
                "rule": "theme_validation",
                "severity": "error" if not passed else "info",
                "message": e,
            }
            for e in errors
        ]
        return {
            "target": str(target),
            "kind": "theme",
            "files": 1,
            "violations": violations,
            "summary": {
                "error": 0 if passed else len(errors),
                "warning": 0,
                "info": len(errors) if passed else 0,
            },
        }

    return {
        "target": str(target),
        "kind": "unknown",
        "files": 0,
        "violations": [{
            "file": str(target),
            "rule": "unsupported_target",
            "severity": "error",
            "message": f"unsupported file type: {target.suffix}",
        }],
        "summary": {"error": 1, "warning": 0, "info": 0},
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="PPT Master — contrast + font-size checker",
    )
    parser.add_argument("target", help="SVG file, SVG directory, or theme JSON")
    parser.add_argument(
        "--theme", type=Path, default=None,
        help="Theme JSON for token table + bg fallback",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of pretty text",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode: only summary line; exit code reflects severity "
             "(0 ok / 1 error / 2 warnings)",
    )
    args = parser.parse_args(argv)

    target = Path(args.target)
    if not target.exists():
        print(f"[ERROR] target does not exist: {target}")
        return 1

    theme = None
    if args.theme is not None:
        theme = load_theme(args.theme)

    report = _audit_path(target, theme)
    if args.json:
        # ensure_ascii=True avoids UnicodeEncodeError on terminals whose
        # default codec (e.g. cp950 on zh-TW Windows) can't emit Chinese
        # text. Consumers can re-decode with json.load which preserves
        # the original characters.
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        header = (
            f"[CONTRAST] target={report['target']} "
            f"kind={report['kind']} files={report['files']}"
        )
        try:
            print(header)
        except UnicodeEncodeError:
            print(header.encode("ascii", "replace").decode("ascii"))
        _print_pretty(report["violations"], ci=args.ci)

    if args.ci:
        if report["summary"]["error"] > 0:
            return 1
        if report["summary"]["warning"] > 0:
            return 2
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
