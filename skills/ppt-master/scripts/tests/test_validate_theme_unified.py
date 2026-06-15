"""Tests for the unified theme validator (post-fix-schema-mismatch).

These tests pin down the unification contract introduced when
``contrast_checker.validate_theme()`` was rewired to dispatch on schema
shape and delegate the modern object-shaped path to ``validate_theme.py``.

Coverage:

  - All four shipped themes in ``skills/ppt-master/themes/*.json`` PASS
    when run through both ``validate_theme.py`` (the canonical validator)
    AND ``contrast_checker.validate_theme_unified`` (the new unified facade).
  - A theme missing ``stroke-frame`` FAILs (object schema requires it).
  - A theme whose ``text-primary`` ratio falls below 4.5 FAILs.
  - Legacy list-shaped fixtures (used by ``test_contrast_checker.py``)
    still go through the legacy path and return ``(True, [])`` for a
    well-formed fixture.
  - A theme with neither list nor object ``ratios`` returns an actionable
    error explaining the schema mismatch.

The tests run via stdlib ``unittest`` (preferred — no third-party deps) or
``python -m pytest scripts/tests/test_validate_theme_unified.py`` if pytest
is available; the helper ``_test_*`` functions at the bottom provide the
pytest-style entry points.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# contrast_checker — for the unified facade.
import contrast_checker  # noqa: E402

# validate_theme (top-level module).
import validate_theme as _vt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
THEMES_DIR = REPO_ROOT / "skills" / "ppt-master" / "themes"
SHIPPED_THEMES = sorted(
    p for p in THEMES_DIR.glob("*.json") if p.name != "_schema.json"
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _build_modern_theme(*, kind: str = "dark",
                        bg: str = "#0A0A0A",
                        text_primary: str = "#F5F5F5",
                        text_secondary: str = "#C8C8C8",
                        text_muted: str = "#8A8A8A",
                        accent: str = "#5AC8FA",
                        accent_warm: str = "#FF9F0A",
                        include_stroke_frame: bool = True,
                        lie_about_text_primary: bool = False
                        ) -> dict:
    """Construct a complete modern-schema theme.

    Used to construct "all tokens present, ratios truthful" (PASS) and
    "missing stroke-frame" / "text-primary below 4.5" / "stated ratio
    diverges from computed" (FAIL) fixtures without touching the shipped
    theme files. Ratios are recomputed from the actual hexes via the
    modern validator's own algorithm — see
    ``validate_theme.contrast_ratio``.
    """
    theme = {
        "id": "test-fixture",
        "kind": kind,
        "version": "1.0.0",
        "description": "synthetic test fixture",
        "tokens": {
            "bg-canvas":      bg,
            "bg-surface":     "#141414" if kind == "dark" else "#F7F7F7",
            "fill-card":      "#1A1A1A" if kind == "dark" else "#EFEFEF",
            "text-primary":   text_primary,
            "text-secondary": text_secondary,
            "text-muted":     text_muted,
            "accent":         accent,
            "accent-warm":    accent_warm,
        },
        "ratios": {},
        "wcag": {
            "min_text_ratio":   4.5,
            "min_stroke_ratio": 3.0,
            "verified_at":      "2026-06-13",
        },
    }
    if include_stroke_frame:
        theme["tokens"]["stroke-frame"]   = "#E5E5E5" if kind == "dark" else "#1A1A1A"
        theme["tokens"]["stroke-divider"] = "#3A3A3A" if kind == "dark" else "#D5D5D5"
    # Recompute ratios from the actual hexes so the modern validator is
    # happy (tolerance ±0.2). The exception is text-primary when the
    # caller wants to test the below-4.5 floor — in that case we lie.
    bg_hex = theme["tokens"]["bg-canvas"]
    for tk in ("text-primary", "text-secondary", "text-muted",
               "accent", "accent-warm"):
        if tk == "text-primary" and lie_about_text_primary:
            theme["ratios"][f"{tk}/bg-canvas"] = 4.5  # lie: it really is <4.5
        else:
            theme["ratios"][f"{tk}/bg-canvas"] = round(
                _vt.contrast_ratio(theme["tokens"][tk], bg_hex), 2
            )
    if include_stroke_frame:
        theme["ratios"]["stroke-frame/bg-canvas"] = round(
            _vt.contrast_ratio(theme["tokens"]["stroke-frame"], bg_hex), 2
        )
        theme["ratios"]["stroke-divider/bg-canvas"] = round(
            _vt.contrast_ratio(theme["tokens"]["stroke-divider"], bg_hex), 2
        )
    return theme


def _write_temp_theme(theme: dict) -> Path:
    fd = tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8"
    )
    json.dump(theme, fd, ensure_ascii=False)
    fd.close()
    return Path(fd.name)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------

class TestShippedThemes(unittest.TestCase):
    """All four shipped themes must PASS both validators."""

    def test_shipped_themes_dir_exists(self):
        self.assertTrue(
            SHIPPED_THEMES,
            msg=f"expected shipped themes in {THEMES_DIR}",
        )

    def test_shipped_themes_pass_validate_theme_py(self):
        for path in SHIPPED_THEMES:
            with self.subTest(theme=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                errs = _vt.validate_theme(data)
                self.assertEqual(
                    errs, [],
                    msg=f"{path.name} should pass validate_theme.py: {errs}",
                )

    def test_shipped_themes_pass_contrast_checker_unified(self):
        for path in SHIPPED_THEMES:
            with self.subTest(theme=path.name):
                passed, errs = contrast_checker.validate_theme_unified(path)
                self.assertTrue(
                    passed,
                    msg=f"{path.name} should pass unified validator: {errs}",
                )
                self.assertEqual(errs, [])

    def test_shipped_themes_pass_contrast_checker_legacy_api(self):
        """The public ``validate_theme`` name should also pass shipped themes."""
        for path in SHIPPED_THEMES:
            with self.subTest(theme=path.name):
                passed, errs = contrast_checker.validate_theme(path)
                self.assertTrue(
                    passed,
                    msg=f"{path.name} should pass legacy API: {errs}",
                )


class TestModernSchemaFailures(unittest.TestCase):
    """Modern-schema fixtures that should FAIL — missing tokens, low contrast."""

    def test_missing_stroke_frame_fails(self):
        theme = _build_modern_theme(include_stroke_frame=False)
        path = _write_temp_theme(theme)
        try:
            passed, errs = contrast_checker.validate_theme_unified(path)
            self.assertFalse(passed, msg=f"expected FAIL, got passed: {errs}")
            self.assertTrue(
                any("stroke-frame" in e for e in errs),
                msg=f"expected 'stroke-frame' in errors, got: {errs}",
            )
        finally:
            path.unlink(missing_ok=True)

    def test_text_primary_below_45_fails(self):
        # text-primary #888888 on bg #0A0A0A ≈ 5.9 — actually clears 4.5.
        # Use a mid-grey closer to the bg to force a sub-4.5 ratio:
        # text-primary #5A5A5A on bg #0A0A0A ≈ 3.31 → FAIL.
        theme = _build_modern_theme(
            kind="dark",
            bg="#0A0A0A",
            text_primary="#5A5A5A",
            text_secondary="#C8C8C8",
            text_muted="#8A8A8A",
            lie_about_text_primary=True,  # force validator to compute and FAIL
        )
        path = _write_temp_theme(theme)
        try:
            passed, errs = contrast_checker.validate_theme_unified(path)
            self.assertFalse(passed, msg=f"expected FAIL, got passed: {errs}")
            # The modern validator flags both the mismatch and the below-
            # threshold ratio. Accept either message form.
            self.assertTrue(
                any(
                    "text-primary" in e and ("below" in e or "mismatch" in e)
                    for e in errs
                ),
                msg=f"expected text-primary failure message in: {errs}",
            )
        finally:
            path.unlink(missing_ok=True)


class TestLegacySchemaStillWorks(unittest.TestCase):
    """Legacy list-shaped fixtures (the test_contrast_checker style) still PASS."""

    def test_legacy_list_shape_passes(self):
        theme = {
            "name": "legacy-fixture",
            "tokens": {
                "bg-canvas":      "#0A0A0A",
                "bg-elevated":    "#141414",
                "text-primary":   "#F5F5F5",
                "text-secondary": "#A8B0D0",
                "accent-frost":   "#3DDDFC",
                "accent-violet":  "#A26BFA",
                "border-subtle":  "#2E3672",
            },
            "ratios": [
                {"pair": ["bg-canvas", "text-primary"],  "ratio": 18.16,
                 "rule": "min_text_ratio"},
                {"pair": ["bg-canvas", "text-secondary"], "ratio": 9.23,
                 "rule": "min_text_ratio"},
                {"pair": ["bg-canvas", "accent-frost"],  "ratio": 12.21,
                 "rule": "min_text_ratio"},
                {"pair": ["bg-elevated", "accent-violet"], "ratio": 5.28,
                 "rule": "min_text_ratio"},
            ],
        }
        path = _write_temp_theme(theme)
        try:
            passed, errs = contrast_checker.validate_theme_unified(path)
            self.assertTrue(passed, msg=f"expected PASS, got: {errs}")
            self.assertEqual(errs, [])
        finally:
            path.unlink(missing_ok=True)


class TestSchemaShapeErrors(unittest.TestCase):
    """Neither shape → actionable error explaining the mismatch."""

    def test_ratios_as_string_fails(self):
        theme = {
            "id": "bad-shape", "kind": "dark", "version": "1.0.0",
            "tokens": {"bg-canvas": "#0A0A0A", "text-primary": "#FFFFFF"},
            "ratios": "not-an-object-or-list",
            "wcag": {"min_text_ratio": 4.5, "min_stroke_ratio": 3.0,
                     "verified_at": "2026-06-13"},
        }
        path = _write_temp_theme(theme)
        try:
            passed, errs = contrast_checker.validate_theme_unified(path)
            self.assertFalse(passed)
            self.assertTrue(
                any("schema" in e or "ratios" in e for e in errs),
                msg=f"expected schema/ratios in errors: {errs}",
            )
        finally:
            path.unlink(missing_ok=True)

    def test_missing_ratios_block_fails(self):
        # No 'ratios' key at all — modern schema requires it.
        theme = {
            "id": "no-ratios", "kind": "dark", "version": "1.0.0",
            "tokens": {"bg-canvas": "#0A0A0A", "text-primary": "#FFFFFF"},
            "wcag": {"min_text_ratio": 4.5, "min_stroke_ratio": 3.0,
                     "verified_at": "2026-06-13"},
        }
        path = _write_temp_theme(theme)
        try:
            passed, errs = contrast_checker.validate_theme_unified(path)
            self.assertFalse(passed)
            self.assertTrue(
                any("ratios" in e for e in errs),
                msg=f"expected 'ratios' in errors: {errs}",
            )
        finally:
            path.unlink(missing_ok=True)

    def test_missing_file_fails(self):
        passed, errs = contrast_checker.validate_theme_unified(
            Path("/nonexistent/theme.json")
        )
        self.assertFalse(passed)
        self.assertTrue(any("not found" in e for e in errs))


# ---------------------------------------------------------------------------
# Pytest-style helpers (for ``python -m pytest`` discovery)
# ---------------------------------------------------------------------------

def test_shipped_themes_all_pass_pytest():
    assert SHIPPED_THEMES, f"no shipped themes at {THEMES_DIR}"
    for path in SHIPPED_THEMES:
        data = json.loads(path.read_text(encoding="utf-8"))
        # canonical validator
        assert _vt.validate_theme(data) == [], (
            f"{path.name} failed canonical validator"
        )
        # unified facade
        passed, errs = contrast_checker.validate_theme_unified(path)
        assert passed, f"{path.name} failed unified validator: {errs}"


def test_missing_stroke_frame_pytest():
    theme = _build_modern_theme(include_stroke_frame=False)
    path = _write_temp_theme(theme)
    try:
        passed, errs = contrast_checker.validate_theme_unified(path)
        assert not passed
        assert any("stroke-frame" in e for e in errs), errs
    finally:
        path.unlink(missing_ok=True)


def test_text_primary_below_45_pytest():
    theme = _build_modern_theme(
        kind="dark", bg="#0A0A0A",
        text_primary="#5A5A5A",
        text_secondary="#C8C8C8",
        text_muted="#8A8A8A",
        lie_about_text_primary=True,
    )
    path = _write_temp_theme(theme)
    try:
        passed, errs = contrast_checker.validate_theme_unified(path)
        assert not passed
        assert any(
            "text-primary" in e and ("below" in e or "mismatch" in e)
            for e in errs
        ), errs
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)