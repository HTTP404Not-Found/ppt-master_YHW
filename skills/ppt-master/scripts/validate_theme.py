#!/usr/bin/env python3
"""
PPT Master - Theme Validator

Standalone validator for `skills/ppt-master/themes/*.json` files. Checks
structural completeness (required top-level keys + required tokens), hex
format, the `kind` enum, and — most importantly — re-computes every
contrast ratio against `bg-canvas` from the WCAG 2.1 relative-luminance
formula and compares to the `ratios` block in the JSON.

This script is the authoritative gate for a theme being shipped. The
authoritative source of truth at runtime is the JSON; this script just
guarantees that the JSON's `ratios` numbers match the actual hex colors,
and that every text token clears WCAG AA (>= 4.5:1) and every stroke-frame
clears 3:1. Decorative `stroke-divider` is allowed to drop below 3:1 (it's
explicitly cosmetic).

Usage:
    python3 scripts/validate_theme.py <theme.json> [<theme2.json> ...]
    python3 scripts/validate_theme.py skills/ppt-master/themes         # validate every *.json
    python3 scripts/validate_theme.py --strict <theme.json>           # also enforce 7:1 AAA on text-primary

Exit codes:
    0  all themes PASS
    1  at least one theme FAILed (structure, hex, ratio mismatch, or WCAG)
    2  bad CLI usage / no themes matched

Dependencies:
    None (Python 3.8+ stdlib only — json, re, argparse, sys, pathlib, math)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — single source of truth for what makes a valid theme.
# Mirrors the requirements in _schema.json but duplicated here so this
# validator can run before jsonschema is available.
# ---------------------------------------------------------------------------

REQUIRED_TOP_LEVEL = ["id", "kind", "version", "tokens", "ratios", "wcag"]
REQUIRED_TOKENS = [
    "bg-canvas",
    "bg-surface",
    "fill-card",
    "text-primary",
    "text-secondary",
    "text-muted",
    "accent",
    "accent-warm",
    "stroke-frame",
    "stroke-divider",
]
REQUIRED_RATIO_KEYS = [
    "text-primary/bg-canvas",
    "text-secondary/bg-canvas",
    "text-muted/bg-canvas",
    "accent/bg-canvas",
    "accent-warm/bg-canvas",
    "stroke-frame/bg-canvas",
    "stroke-divider/bg-canvas",
]
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")

# Text + stroke-frame tokens must clear WCAG AA text thresholds.
TEXT_TOKEN_FLOOR = 4.5
STROKE_FRAME_FLOOR = 3.0
# Accent saturation guardrail — too bright and it glares on dark themes.
ACCENT_CEILING = 12.0
# Tolerated |stated - computed| when checking the ratios block.
RATIO_TOLERANCE = 0.2


# ---------------------------------------------------------------------------
# WCAG 2.1 relative-luminance helpers (stdlib only).
# ---------------------------------------------------------------------------

def _srgb_to_linear(channel: float) -> float:
    """Single-channel sRGB reverse-gamma per WCAG 2.x."""
    if channel <= 0.03928:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    """Return WCAG relative luminance for a `#RRGGBB` (or `#RGB`) string."""
    s = hex_color.lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
        raise ValueError(f"not a hex color: {hex_color!r}")
    r, g, b = (int(s[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (
        0.2126 * _srgb_to_linear(r)
        + 0.7152 * _srgb_to_linear(g)
        + 0.0722 * _srgb_to_linear(b)
    )


def contrast_ratio(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two hex colors — `(L1 + 0.05) / (L2 + 0.05)`."""
    lf, lb = relative_luminance(fg), relative_luminance(bg)
    lighter, darker = max(lf, lb), min(lf, lb)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------------------
# Validation core.
# ---------------------------------------------------------------------------

def _err(errors: list[str], msg: str) -> None:
    errors.append(msg)


def validate_theme(theme: dict, *, strict: bool = False) -> list[str]:
    """
    Return a list of human-readable error messages (empty list = PASS).

    Checks performed, in order:
      1. Top-level keys present.
      2. `id` is kebab-case, `kind` in {dark, light}, `version` is semver.
      3. `tokens` covers all REQUIRED_TOKENS, each value is `#RRGGBB`.
      4. `ratios` covers all REQUIRED_RATIO_KEYS, each value is a number.
      5. Re-computed ratios match the stated values within RATIO_TOLERANCE.
      6. All text tokens clear TEXT_TOKEN_FLOOR against `bg-canvas`.
      7. `stroke-frame` clears STROKE_FRAME_FLOOR.
      8. Accent tokens are within [TEXT_TOKEN_FLOOR, ACCENT_CEILING].
      9. `wcag.min_text_ratio == 4.5`, `wcag.min_stroke_ratio == 3.0`.
     10. (strict) `text-primary` ratio >= 7.0 (AAA).
    """
    errors: list[str] = []

    # 1. Top-level structure
    for key in REQUIRED_TOP_LEVEL:
        if key not in theme:
            _err(errors, f"missing required top-level key: {key!r}")

    # 2. Field-level constraints
    tid = theme.get("id")
    if not isinstance(tid, str) or not ID_RE.match(tid or ""):
        _err(errors, f"`id` must be kebab-case string (got {tid!r})")
    if theme.get("kind") not in ("dark", "light"):
        _err(errors, f"`kind` must be 'dark' or 'light' (got {theme.get('kind')!r})")
    ver = theme.get("version")
    if not isinstance(ver, str) or not SEMVER_RE.match(ver or ""):
        _err(errors, f"`version` must be semver (got {ver!r})")

    # 3. Tokens
    tokens = theme.get("tokens")
    if not isinstance(tokens, dict):
        _err(errors, "`tokens` must be an object")
    else:
        for t in REQUIRED_TOKENS:
            if t not in tokens:
                _err(errors, f"missing required token: {t!r}")
        for t, v in tokens.items():
            if t not in REQUIRED_TOKENS:
                _err(errors, f"unknown token: {t!r}")
            elif not isinstance(v, str) or not HEX_RE.match(v):
                _err(errors, f"token {t!r} must be #RRGGBB hex (got {v!r})")

    # Stop early — further checks depend on a valid bg-canvas.
    if not isinstance(tokens, dict) or not HEX_RE.match(tokens.get("bg-canvas", "")):
        return errors

    bg = tokens["bg-canvas"]

    # 4. Ratios block completeness
    ratios = theme.get("ratios")
    if not isinstance(ratios, dict):
        _err(errors, "`ratios` must be an object")
        return errors
    for r in REQUIRED_RATIO_KEYS:
        if r not in ratios:
            _err(errors, f"missing required ratio key: {r!r}")
    for k, v in ratios.items():
        if k not in REQUIRED_RATIO_KEYS:
            _err(errors, f"unknown ratio key: {k!r}")
        elif not isinstance(v, (int, float)):
            _err(errors, f"ratio {k!r} must be a number (got {v!r})")

    # 5. Recompute and compare
    PAIRS = {
        "text-primary/bg-canvas":   "text-primary",
        "text-secondary/bg-canvas": "text-secondary",
        "text-muted/bg-canvas":     "text-muted",
        "accent/bg-canvas":         "accent",
        "accent-warm/bg-canvas":    "accent-warm",
        "stroke-frame/bg-canvas":   "stroke-frame",
        "stroke-divider/bg-canvas": "stroke-divider",
    }
    for ratio_key, token_name in PAIRS.items():
        if ratio_key not in ratios or not isinstance(ratios[ratio_key], (int, float)):
            continue
        if token_name not in tokens:
            continue
        computed = contrast_ratio(tokens[token_name], bg)
        stated = ratios[ratio_key]
        if abs(stated - computed) > RATIO_TOLERANCE:
            _err(
                errors,
                f"ratio mismatch {ratio_key}: stated={stated:.2f} "
                f"computed={computed:.2f} (tol={RATIO_TOLERANCE})",
            )

    # 6-8. Threshold checks
    for tk in ("text-primary", "text-secondary", "text-muted"):
        ratio_key = f"{tk}/bg-canvas"
        if ratio_key not in ratios or not isinstance(ratios[ratio_key], (int, float)):
            continue
        if ratios[ratio_key] < TEXT_TOKEN_FLOOR:
            _err(
                errors,
                f"{ratio_key} = {ratios[ratio_key]:.2f} is below WCAG AA "
                f"text floor {TEXT_TOKEN_FLOOR}",
            )

    sf_ratio = ratios.get("stroke-frame/bg-canvas")
    if isinstance(sf_ratio, (int, float)) and sf_ratio < STROKE_FRAME_FLOOR:
        _err(
            errors,
            f"stroke-frame/bg-canvas = {sf_ratio:.2f} is below WCAG non-text "
            f"floor {STROKE_FRAME_FLOOR}",
        )

    for tk in ("accent", "accent-warm"):
        ratio_key = f"{tk}/bg-canvas"
        if ratio_key not in ratios or not isinstance(ratios[ratio_key], (int, float)):
            continue
        v = ratios[ratio_key]
        if v < TEXT_TOKEN_FLOOR:
            _err(
                errors,
                f"{ratio_key} = {v:.2f} is below WCAG AA text floor "
                f"{TEXT_TOKEN_FLOOR}",
            )
        if v > ACCENT_CEILING:
            _err(
                errors,
                f"{ratio_key} = {v:.2f} exceeds accent-glare ceiling "
                f"{ACCENT_CEILING} (accent too bright vs bg-canvas)",
            )

    # 9. wcag block
    wcag = theme.get("wcag")
    if not isinstance(wcag, dict):
        _err(errors, "`wcag` must be an object")
    else:
        if wcag.get("min_text_ratio") != 4.5:
            _err(errors, f"`wcag.min_text_ratio` must be 4.5 (got {wcag.get('min_text_ratio')!r})")
        if wcag.get("min_stroke_ratio") != 3.0:
            _err(errors, f"`wcag.min_stroke_ratio` must be 3.0 (got {wcag.get('min_stroke_ratio')!r})")
        if not isinstance(wcag.get("verified_at"), str):
            _err(errors, "`wcag.verified_at` must be ISO date string")

    # 10. Strict mode — AAA on primary text.
    if strict:
        tp = ratios.get("text-primary/bg-canvas")
        if isinstance(tp, (int, float)) and tp < 7.0:
            _err(
                errors,
                f"strict mode: text-primary/bg-canvas = {tp:.2f} below AAA 7.0",
            )

    return errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _discover_themes(path: Path) -> list[Path]:
    """If path is a directory, return all *.json in it (skipping _schema.json)."""
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(
            p for p in path.glob("*.json") if p.name != "_schema.json"
        )
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate ppt-master theme JSON files. Re-computes WCAG contrast "
            "ratios from hex values and compares to the stated ratios block."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="One or more theme.json paths or a directory of themes.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also enforce WCAG AAA (text-primary ratio >= 7.0).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON summary (one object per file).",
    )
    args = parser.parse_args(argv)

    # Expand any directory args.
    themes: list[Path] = []
    for p in args.paths:
        discovered = _discover_themes(p)
        if not discovered:
            print(f"warning: no theme JSON files at {p}", file=sys.stderr)
        themes.extend(discovered)

    if not themes:
        print("error: no theme files to validate", file=sys.stderr)
        return 2

    results = []
    any_fail = False
    for path in themes:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            results.append({"path": str(path), "passed": False, "errors": [f"read/parse: {exc}"]})
            any_fail = True
            continue
        errs = validate_theme(data, strict=args.strict)
        passed = not errs
        if not passed:
            any_fail = True
        results.append(
            {
                "path": str(path),
                "id": data.get("id") if isinstance(data, dict) else None,
                "passed": passed,
                "errors": errs,
            }
        )

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for r in results:
            tag = "PASS" if r["passed"] else "FAIL"
            print(f"[{tag}] {r['path']}" + (f"  (id={r['id']})" if r.get("id") else ""))
            for e in r["errors"]:
                print(f"    - {e}")

    # Summary line — helps CI / make targets parse pass/fail at a glance.
    total = len(results)
    passed_count = sum(1 for r in results if r["passed"])
    print(f"\nSummary: {passed_count}/{total} themes passed.")

    return 0 if not any_fail else 1


if __name__ == "__main__":
    sys.exit(main())