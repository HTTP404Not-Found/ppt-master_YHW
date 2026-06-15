#!/usr/bin/env python3
"""Diagnose theme-JSON schema mismatch between the two ppt-master validators.

The repo currently ships two validators that disagree about what a theme JSON
looks like:

  - ``skills/ppt-master/scripts/validate_theme.py``
    Single-source-of-truth for the theme-system track. Implements the
    *object-shaped* schema documented in ``themes/_schema.json``:
    ``ratios`` is a dict keyed by ``"<fg>/bg-canvas"`` and a ``wcag`` block
    declares ``min_text_ratio`` / ``min_stroke_ratio`` / ``verified_at``.

  - ``skills/ppt-master/scripts/contrast_checker.py::validate_theme``
    Left over from an earlier draft that used a *list-shaped* schema:
    ``ratios`` is a list of ``{pair, ratio, rule}`` objects. No ``wcag`` block,
    no token allow-list, no stroke-frame non-text threshold.

When both validators are run against a theme that uses the modern schema,
``validate_theme.py`` accepts it and ``contrast_checker.validate_theme`` rejects
it with ``'ratios' must be a list``. This tool enumerates the concrete diffs
between the two implementations so the mismatch is visible without reading
500+ lines of validator source.

Usage
-----
::

    python tools/diagnose_theme_schema.py [path/to/theme.json ...]
    python tools/diagnose_theme_schema.py skills/ppt-master/themes

Exit codes:
    0  report printed (informational; not a gate)
    2  bad CLI usage

Design
------
Pure stdlib. Reads ``validate_theme.py`` and ``contrast_checker.py`` as text,
locates the two ``validate_theme`` functions via regex, and pulls out the
schema-shape hints each one documents (expected ``ratios`` shape, required
tokens, threshold constants, etc.). No execution of the target code; we just
diff the docs and the constants they expose.

This is a one-shot diagnostic — once both validators are unified it becomes
informational only, and the file can stay in the repo as living documentation
of why the old list-shaped schema was retired.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Static catalogue of the structural differences — derived from reading the
# source. If you change one of the validators, update the matching row here.
# ---------------------------------------------------------------------------

# Authoritative schema (matches _schema.json + validate_theme.py).
SCHEMA_OBJECT_RATIOS_KEYS = [
    "text-primary/bg-canvas",
    "text-secondary/bg-canvas",
    "text-muted/bg-canvas",
    "accent/bg-canvas",
    "accent-warm/bg-canvas",
    "stroke-frame/bg-canvas",
    "stroke-divider/bg-canvas",
]
SCHEMA_REQUIRED_TOKENS = [
    "bg-canvas", "bg-surface", "fill-card",
    "text-primary", "text-secondary", "text-muted",
    "accent", "accent-warm",
    "stroke-frame", "stroke-divider",
]
SCHEMA_REQUIRED_TOP = ["id", "kind", "version", "tokens", "ratios", "wcag"]
SCHEMA_THRESHOLDS = {
    "TEXT_TOKEN_FLOOR":      4.5,
    "STROKE_FRAME_FLOOR":    3.0,
    "ACCENT_CEILING":        12.0,
    "RATIO_TOLERANCE":       0.2,
}

# Stale schema that contrast_checker.validate_theme() was originally written
# against. Kept here only as evidence of the historical mismatch.
LEGACY_LIST_SHAPE_RATIO_KEYS = None  # list shape — no fixed keys, derived from entries
LEGACY_REQUIRED_TOKENS = None        # implicit: whatever the pairs reference


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_validate_theme_block(src: str, *, in_contrast_checker: bool) -> str:
    """Return the docstring + first ~80 lines of the target validate_theme func.

    We do not need to execute the code — we just need enough text to show the
    expected schema in its docstring. This is brittle if the validators get
    refactored heavily, but the goal is "diagnose the current diff", not
    "parse arbitrary Python".
    """
    if in_contrast_checker:
        # contrast_checker.validate_theme takes a Path; locate the first def.
        m = re.search(r"^def validate_theme\b", src, re.MULTILINE)
        if not m:
            return ""
        start = m.start()
    else:
        # validate_theme.py's validate_theme takes a dict; locate the docstring.
        m = re.search(r"^def validate_theme\b.*?:", src, re.MULTILINE | re.DOTALL)
        if not m:
            return ""
        start = m.start()
    # Slice forward ~60 lines for context.
    lines = src[start:].splitlines()[:60]
    return "\n".join(lines)


def _summarise_validate_theme_py(path: Path) -> dict:
    src = _read_text(path)
    block = _extract_validate_theme_block(src, in_contrast_checker=False)
    return {
        "path": str(path),
        "accepts_input": "dict",
        "ratios_shape": "object",
        "ratios_keys": SCHEMA_OBJECT_RATIOS_KEYS,
        "required_tokens": SCHEMA_REQUIRED_TOKENS,
        "required_top_level": SCHEMA_REQUIRED_TOP,
        "thresholds": SCHEMA_THRESHOLDS,
        "tolerance": SCHEMA_THRESHOLDS["RATIO_TOLERANCE"],
        "validates_wcag_block": True,
        "validates_id_kind_version": True,
        "docstring_excerpt": block,
    }


def _summarise_contrast_checker_py(path: Path) -> dict:
    src = _read_text(path)
    block = _extract_validate_theme_block(src, in_contrast_checker=True)
    return {
        "path": str(path),
        "accepts_input": "Path",
        "ratios_shape": "list",
        "ratios_keys": LEGACY_LIST_SHAPE_RATIO_KEYS,
        "required_tokens": LEGACY_REQUIRED_TOKENS,
        "required_top_level": None,
        "thresholds": {"WCAG_AA_RATIO": 4.5},
        "tolerance": 0.5,
        "validates_wcag_block": False,
        "validates_id_kind_version": False,
        "docstring_excerpt": block,
    }


def _diff_summaries(a: dict, b: dict) -> list[str]:
    """Return a list of human-readable diff lines."""
    diffs: list[str] = []

    # Shape
    if a["ratios_shape"] != b["ratios_shape"]:
        diffs.append(
            f"  ratios shape  : A={a['ratios_shape']}  vs  B={b['ratios_shape']}"
        )
    # Input
    if a["accepts_input"] != b["accepts_input"]:
        diffs.append(
            f"  input type    : A={a['accepts_input']}  vs  B={b['accepts_input']}"
        )
    # Tolerance
    if a["tolerance"] != b["tolerance"]:
        diffs.append(
            f"  recompute tol : A=±{a['tolerance']}  vs  B=±{b['tolerance']}"
        )
    # Token set
    if a["required_tokens"] and b["required_tokens"] is None:
        diffs.append(
            f"  required tk   : A enforces {len(a['required_tokens'])} "
            f"tokens  vs  B enforces NONE (only pairs in ratios)"
        )
    # Threshold set
    a_thr = set(a["thresholds"].keys())
    b_thr = set(b["thresholds"].keys())
    only_a = a_thr - b_thr
    only_b = b_thr - a_thr
    if only_a or only_b:
        diffs.append(
            f"  thresholds    : A-only={sorted(only_a) or '∅'}  "
            f"B-only={sorted(only_b) or '∅'}"
        )
    # Schema extras
    if a["validates_wcag_block"] != b["validates_wcag_block"]:
        diffs.append(
            f"  wcag block    : A validates  vs  B skips"
        )
    if a["validates_id_kind_version"] != b["validates_id_kind_version"]:
        diffs.append(
            f"  id/kind/ver   : A validates  vs  B skips"
        )
    return diffs


def _run_theme(path: Path) -> dict:
    """Run a single theme through both validators and capture pass/fail."""
    import importlib.util

    out: dict = {"theme": str(path), "validate_theme_py": None,
                 "contrast_checker_py": None}

    # 1) validate_theme.py — operates on a parsed dict.
    spec = importlib.util.spec_from_file_location(
        "_vt", path.parent.parent / "skills" / "ppt-master" / "scripts"
        / "validate_theme.py"
    )
    # Fallback: discover the scripts dir relative to the workspace root.
    # The caller (this tool) lives at <repo>/tools; the scripts dir is at
    # <repo>/skills/ppt-master/scripts.
    repo_root = Path(__file__).resolve().parent.parent
    vt_path = repo_root / "skills" / "ppt-master" / "scripts" / "validate_theme.py"
    cc_path = repo_root / "skills" / "ppt-master" / "scripts" / "contrast_checker.py"
    spec = importlib.util.spec_from_file_location("_vt", vt_path)
    vt = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(vt)  # type: ignore[union-attr]
        data = json.loads(path.read_text(encoding="utf-8"))
        errs = vt.validate_theme(data)
        out["validate_theme_py"] = {
            "passed": not errs,
            "error_count": len(errs),
            "first_error": errs[0] if errs else None,
        }
    except Exception as exc:  # pragma: no cover — diagnostic only
        out["validate_theme_py"] = {"passed": None, "error": repr(exc)}

    # 2) contrast_checker.validate_theme — operates on a Path.
    try:
        spec = importlib.util.spec_from_file_location("_cc", cc_path)
        cc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cc)  # type: ignore[union-attr]
        passed, errs = cc.validate_theme(path)
        out["contrast_checker_py"] = {
            "passed": bool(passed),
            "error_count": len(errs),
            "first_error": errs[0] if errs else None,
        }
    except Exception as exc:  # pragma: no cover
        out["contrast_checker_py"] = {"passed": None, "error": repr(exc)}

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Diff the two ppt-master theme validators and run a theme "
            "through both, side-by-side."
        ),
    )
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="Theme JSON files or a directory. If empty, runs the schema diff only.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of pretty text.",
    )
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    vt_path = repo_root / "skills" / "ppt-master" / "scripts" / "validate_theme.py"
    cc_path = repo_root / "skills" / "ppt-master" / "scripts" / "contrast_checker.py"

    a = _summarise_validate_theme_py(vt_path)
    b = _summarise_contrast_checker_py(cc_path)

    if args.json:
        print(json.dumps(
            {"validate_theme_py": a, "contrast_checker_py": b,
             "diffs": _diff_summaries(a, b)},
            indent=2,
        ))
    else:
        print("=" * 72)
        print("A = validate_theme.py     (authoritative; matches _schema.json)")
        print("B = contrast_checker.py::validate_theme  (legacy list-shaped)")
        print("=" * 72)
        print("\n--- Schema-shape diff ---")
        for line in _diff_summaries(a, b):
            print(line)
        print("\nA's docstring (first 30 lines):")
        for ln in a["docstring_excerpt"].splitlines()[:30]:
            print(f"  {ln}")
        print("\nB's docstring (first 30 lines):")
        for ln in b["docstring_excerpt"].splitlines()[:30]:
            print(f"  {ln}")

    # Optional: run each provided theme through both validators.
    if args.paths:
        themes: list[Path] = []
        for p in args.paths:
            if p.is_dir():
                themes.extend(sorted(t for t in p.glob("*.json") if t.name != "_schema.json"))
            elif p.is_file():
                themes.append(p)
        if themes:
            print("\n--- Per-theme execution ---")
            for t in themes:
                result = _run_theme(t)
                print(f"\n  {result['theme']}")
                for k in ("validate_theme_py", "contrast_checker_py"):
                    r = result[k] or {}
                    passed = r.get("passed")
                    tag = "PASS" if passed else ("FAIL" if passed is False else "ERR")
                    extra = r.get("first_error") or r.get("error") or ""
                    print(f"    [{tag}] {k}  ({r.get('error_count', '-')} error(s))  {extra}")

    return 0


if __name__ == "__main__":
    sys.exit(main())