"""Tests for contrast_checker.py.

Run via ``python -m pytest scripts/tests/test_contrast_checker.py`` (preferred)
or ``python -m unittest scripts.tests.test_contrast_checker`` (stdlib fallback
when pytest is not installed).

The test suite uses both unittest discovery and pytest naming conventions
so both runners work. No third-party deps; fixtures are constructed inline
to avoid coupling to a fixed directory layout (the themes/ tree belongs
to another track and isn't shipped here yet).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure scripts/ is on sys.path regardless of invocation directory.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from contrast_checker import (  # noqa: E402
    compute_ratio,
    validate_theme,
    check_font_sizes,
    check_no_hex_literals,
    check_svg,
    expand_tokens,
    severity_for_ratio,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _build_dark_frost() -> dict:
    """The dark-frost theme described in plan.yaml §1.

    bg-canvas #0A0A0A vs text-primary #F5F5F5 ≈ 18:1 (WCAG AAA on both axes).
    Plus a couple of accent pairs that also pass comfortably. Ratios are
    declared *within the recompute tolerance* (0.5) of the actual values so
    ``validate_theme``'s divergence guard does not false-positive.
    """
    return {
        "name": "dark-frost",
        "description": "黑底 + 高對比白字 + 冰藍強調",
        "bg": "#0A0A0A",
        "tokens": {
            "bg-canvas": "#0A0A0A",
            "bg-elevated": "#141414",
            "text-primary": "#F5F5F5",
            "text-secondary": "#A8B0D0",
            "accent-frost": "#3DDDFC",
            "accent-violet": "#A26BFA",
            "border-subtle": "#2E3672",
        },
        # Real recomputed ratios: 18.16 / 9.23 / 12.21 / 5.28. Declared
        # values sit within the 0.5 tolerance so the divergence guard is a
        # no-op here.
        "ratios": [
            {"pair": ["bg-canvas", "text-primary"], "ratio": 18.16,
             "rule": "min_text_ratio"},
            {"pair": ["bg-canvas", "text-secondary"], "ratio": 9.23,
             "rule": "min_text_ratio"},
            {"pair": ["bg-canvas", "accent-frost"], "ratio": 12.21,
             "rule": "min_text_ratio"},
            {"pair": ["bg-elevated", "accent-violet"], "ratio": 5.28,
             "rule": "min_text_ratio"},
        ],
    }


def _make_temp_json(payload: dict) -> Path:
    fd = tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8"
    )
    json.dump(payload, fd, ensure_ascii=False)
    fd.close()
    return Path(fd.name)


def _make_temp_svg(content: str) -> Path:
    fd = tempfile.NamedTemporaryFile(
        suffix=".svg", delete=False, mode="w", encoding="utf-8"
    )
    fd.write(content)
    fd.close()
    return Path(fd.name)


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------

class TestComputeRatio(unittest.TestCase):
    def test_white_vs_black(self):
        # White vs black = 21:1 (the canonical maximum).
        self.assertAlmostEqual(compute_ratio("#FFFFFF", "#000000"), 21.0, places=1)

    def test_compute_ratio_short_hex(self):
        # #FFF vs #000 should produce the same 21:1.
        self.assertAlmostEqual(compute_ratio("#FFF", "#000"), 21.0, places=1)

    def test_identical_colors(self):
        # Identical fg/bg collapses to 1:1.
        self.assertAlmostEqual(compute_ratio("#888888", "#888888"), 1.0, places=2)

    def test_symmetric(self):
        # Order shouldn't matter — ratio(L1, L2) == ratio(L2, L1).
        a = compute_ratio("#5B8DEF", "#0A0E27")
        b = compute_ratio("#0A0E27", "#5B8DEF")
        self.assertAlmostEqual(a, b, places=6)

    def test_severity_ladder(self):
        # 3.0 → error, 5.0 → warning, 8.0 → ok
        self.assertEqual(severity_for_ratio(3.0), "error")
        self.assertEqual(severity_for_ratio(4.4), "error")
        self.assertEqual(severity_for_ratio(4.5), "warning")
        self.assertEqual(severity_for_ratio(6.99), "warning")
        self.assertEqual(severity_for_ratio(7.0), "ok")
        self.assertEqual(severity_for_ratio(21.0), "ok")

    def test_invalid_input_raises(self):
        with self.assertRaises(ValueError):
            compute_ratio("not-a-hex", "#000000")
        with self.assertRaises(ValueError):
            compute_ratio("#FFFFFF", "white")


# ---------------------------------------------------------------------------
# Theme validation
# ---------------------------------------------------------------------------

class TestValidateTheme(unittest.TestCase):
    def test_validate_theme_pass(self):
        path = _make_temp_json(_build_dark_frost())
        try:
            passed, errors = validate_theme(path)
            self.assertTrue(passed, msg=f"expected pass, got errors: {errors}")
            self.assertEqual(errors, [])
        finally:
            path.unlink(missing_ok=True)

    def test_validate_theme_fail(self):
        # Same theme but text-primary knocked down to mid grey — fails WCAG AA
        # on the bg-canvas vs text-primary pair.
        theme = _build_dark_frost()
        theme["tokens"]["text-primary"] = "#888888"
        # And lie about its ratio so the recompute catches it.
        theme["ratios"][0]["ratio"] = 17.0
        path = _make_temp_json(theme)
        try:
            passed, errors = validate_theme(path)
            self.assertFalse(passed)
            self.assertTrue(
                any("WCAG AA" in e or "diverges" in e for e in errors),
                msg=f"unexpected errors: {errors}",
            )
        finally:
            path.unlink(missing_ok=True)

    def test_validate_theme_missing_file(self):
        passed, errors = validate_theme(Path("/nonexistent/theme.json"))
        self.assertFalse(passed)
        self.assertTrue(any("not found" in e for e in errors))


# ---------------------------------------------------------------------------
# Font-size audit
# ---------------------------------------------------------------------------

# Spec_lock payload used by check_font_sizes for these tests.
_FROST_LOCK = """\
# Execution Lock

## colors
- bg: #0A0A0A

## typography
- body: 24
- title: 48
- subtitle: 32
- annotation: 13
"""


class TestCheckFontSizes(unittest.TestCase):
    def _lock_path(self) -> Path:
        """Write a fresh ``spec_lock.md`` next to ``tmp<id>.svg`` and return
        the lock path. ``check_svg`` / ``check_font_sizes`` look for the
        file named ``spec_lock.md`` in the SVG's directory; renaming the
        tempfile that way ensures the lookup hits.
        """
        # Use a stable filename inside a fresh temp directory.
        tmpdir = Path(tempfile.mkdtemp(prefix="contrast_test_"))
        lock = tmpdir / "spec_lock.md"
        lock.write_text(_FROST_LOCK, encoding="utf-8")
        return lock

    def _cleanup(self, lock_path: Path) -> None:
        lock_path.unlink(missing_ok=True)
        if lock_path.parent.exists():
            for child in lock_path.parent.iterdir():
                child.unlink(missing_ok=True)
            try:
                lock_path.parent.rmdir()
            except OSError:
                pass

    def test_check_font_sizes(self):
        lock_path = self._lock_path()
        # Place SVG next to the lock so the resolver finds it.
        svg = lock_path.parent / "tmp_font.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '  <g id="content">'
            '    <text x="0" y="0" font-size="18">Too small</text>'
            '  </g>'
            '</svg>',
            encoding="utf-8",
        )
        try:
            violations = check_font_sizes(svg)
            errors = [v for v in violations if v.get("severity") == "error"]
            self.assertTrue(
                errors, msg=f"expected an error, got: {violations}"
            )
            self.assertEqual(errors[0]["font_size"], 18)
            self.assertEqual(errors[0]["role"], "body")
        finally:
            self._cleanup(lock_path)

    def test_check_font_sizes_h1_pass(self):
        lock_path = self._lock_path()
        svg = lock_path.parent / "tmp_h1.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '  <g id="cover-title">'
            '    <text x="0" y="0" font-size="44">Big Heading</text>'
            '  </g>'
            '</svg>',
            encoding="utf-8",
        )
        try:
            violations = check_font_sizes(svg)
            errors = [v for v in violations if v.get("severity") == "error"]
            self.assertFalse(
                errors, msg=f"unexpected error violations: {errors}"
            )
        finally:
            self._cleanup(lock_path)

    def test_check_font_sizes_h2_below_ramp(self):
        # H2 ramp is 32 by default; 28 should flag.
        # Use a section heading id that does NOT contain "title" so the
        # role classifier does not misroute h2 to h1.
        lock_path = self._lock_path()
        svg = lock_path.parent / "tmp_h2.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '  <g id="subtitle-section">'
            '    <text x="0" y="0" font-size="28">Subtitle</text>'
            '  </g>'
            '</svg>',
            encoding="utf-8",
        )
        try:
            violations = check_font_sizes(svg)
            errors = [v for v in violations if v.get("severity") == "error"]
            self.assertTrue(errors)
            # role classification: 'subtitle' triggers h2.
            self.assertEqual(errors[0]["role"], "h2")
        finally:
            self._cleanup(lock_path)


# ---------------------------------------------------------------------------
# No-hex-literal scan
# ---------------------------------------------------------------------------

class TestCheckNoHexLiterals(unittest.TestCase):
    def test_no_hex_violation(self):
        theme = _build_dark_frost()
        # Use a hex that's NOT in the theme's tokens table.
        svg = _make_temp_svg(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '  <rect x="0" y="0" width="100" height="100" fill="#1A1A1A"/>'
            '</svg>'
        )
        try:
            violations = check_no_hex_literals(svg, theme=theme)
            self.assertTrue(violations, msg="expected #1A1A1A to be flagged")
            self.assertEqual(violations[0]["color"], "#1A1A1A")
        finally:
            svg.unlink(missing_ok=True)

    def test_theme_token_hex_allowed(self):
        theme = _build_dark_frost()
        svg = _make_temp_svg(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '  <rect x="0" y="0" width="100" height="100" fill="#0A0A0A"/>'
            '  <text x="0" y="50" font-size="32" fill="#F5F5F5">Hi</text>'
            '</svg>'
        )
        try:
            violations = check_no_hex_literals(svg, theme=theme)
            self.assertEqual(
                violations, [],
                msg=f"theme tokens should be allowed; got: {violations}",
            )
        finally:
            svg.unlink(missing_ok=True)

    def test_named_colors_always_allowed(self):
        svg = _make_temp_svg(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '  <rect x="0" y="0" width="100" height="100" fill="white"/>'
            '  <rect x="0" y="0" width="100" height="100" fill="transparent"/>'
            '  <rect x="0" y="0" width="100" height="100" fill="currentColor"/>'
            '  <rect x="0" y="0" width="100" height="100" fill="none"/>'
            '</svg>'
        )
        try:
            violations = check_no_hex_literals(svg)
            self.assertEqual(violations, [])
        finally:
            svg.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# check_svg (contrast on fill/stroke against bg)
# ---------------------------------------------------------------------------

class TestCheckSvg(unittest.TestCase):
    def _lock_path(self) -> Path:
        """Write a fresh ``spec_lock.md`` into a temp directory."""
        tmpdir = Path(tempfile.mkdtemp(prefix="contrast_test_"))
        lock = tmpdir / "spec_lock.md"
        lock.write_text(_FROST_LOCK, encoding="utf-8")
        return lock

    def _cleanup(self, lock_path: Path) -> None:
        lock_path.unlink(missing_ok=True)
        if lock_path.parent.exists():
            for child in lock_path.parent.iterdir():
                child.unlink(missing_ok=True)
            try:
                lock_path.parent.rmdir()
            except OSError:
                pass

    def test_low_contrast_text_error(self):
        # Mid-grey text on near-black bg — fails WCAG AA.
        lock = self._lock_path()
        svg = lock.parent / "tmp_svg.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '  <g id="content">'
            '    <text x="0" y="50" font-size="32" fill="#3A3A3A">Hi</text>'
            '  </g>'
            '</svg>',
            encoding="utf-8",
        )
        try:
            v = check_svg(svg)
            errors = [x for x in v if x.get("severity") == "error"]
            self.assertTrue(errors, msg=f"expected error, got: {v}")
            # #3A3A3A vs #0A0A0A → ratio ≈ 1.59 (very low contrast).
            self.assertAlmostEqual(errors[0]["ratio"], 1.6, delta=0.5)
        finally:
            self._cleanup(lock)

    def test_high_contrast_text_ok(self):
        lock = self._lock_path()
        svg = lock.parent / "tmp_svg_ok.svg"
        svg.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
            '  <g id="content">'
            '    <text x="0" y="50" font-size="32" fill="#F5F5F5">Hi</text>'
            '  </g>'
            '</svg>',
            encoding="utf-8",
        )
        try:
            v = check_svg(svg)
            # All entries should be ok or warning — no errors expected.
            errors = [x for x in v if x.get("severity") == "error"]
            self.assertFalse(errors, msg=f"unexpected errors: {errors}")
        finally:
            self._cleanup(lock)


# ---------------------------------------------------------------------------
# Token expansion
# ---------------------------------------------------------------------------

class TestExpandTokens(unittest.TestCase):
    def test_expand_known_tokens(self):
        theme = _build_dark_frost()
        content = (
            '<svg><rect fill="var(--bg-canvas)"/>'
            '<text fill="var(--text-primary)">Hi</text></svg>'
        )
        out, warnings = expand_tokens(content, theme)
        self.assertIn("#0A0A0A", out)
        self.assertIn("#F5F5F5", out)
        self.assertNotIn("var(--bg-canvas)", out)
        self.assertEqual(warnings, [])

    def test_expand_unknown_token_warns(self):
        theme = _build_dark_frost()
        content = '<svg><rect fill="var(--does-not-exist)"/></svg>'
        out, warnings = expand_tokens(content, theme)
        self.assertIn("var(--does-not-exist)", out)
        self.assertTrue(any("does-not-exist" in w for w in warnings))

    def test_expand_no_tokens_is_passthrough(self):
        content = '<svg><rect fill="#112233"/></svg>'
        out, warnings = expand_tokens(content, {"tokens": {}})
        self.assertEqual(out, content)
        self.assertEqual(warnings, [])


# ---------------------------------------------------------------------------
# Pytest-style helpers (for ``python -m pytest`` discovery).
# ---------------------------------------------------------------------------

def test_compute_ratio_white_black_pytest():
    assert abs(compute_ratio("#FFFFFF", "#000000") - 21.0) < 0.1


def test_compute_ratio_short_hex_pytest():
    assert abs(compute_ratio("#FFF", "#000") - 21.0) < 0.1


def test_validate_theme_pass_pytest():
    path = _make_temp_json(_build_dark_frost())
    try:
        passed, errors = validate_theme(path)
        assert passed, f"expected pass, got errors: {errors}"
        assert errors == []
    finally:
        path.unlink(missing_ok=True)


def test_validate_theme_fail_pytest():
    theme = _build_dark_frost()
    theme["tokens"]["text-primary"] = "#888888"
    path = _make_temp_json(theme)
    try:
        passed, errors = validate_theme(path)
        assert not passed
        assert any("WCAG AA" in e or "diverges" in e for e in errors)
    finally:
        path.unlink(missing_ok=True)


def test_check_font_sizes_pytest():
    lock = _make_temp_svg(_FROST_LOCK).with_suffix(".md")
    svg = lock.with_suffix(".svg")
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '  <g id="content">'
        '    <text x="0" y="0" font-size="18">Too small</text>'
        '  </g>'
        '</svg>',
        encoding="utf-8",
    )
    try:
        v = check_font_sizes(svg)
        assert any(x.get("severity") == "error" for x in v)
    finally:
        svg.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def test_check_font_sizes_h1_pass_pytest():
    lock = _make_temp_svg(_FROST_LOCK).with_suffix(".md")
    svg = lock.with_suffix(".svg")
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '  <g id="cover-title">'
        '    <text x="0" y="0" font-size="44">Big Heading</text>'
        '  </g>'
        '</svg>',
        encoding="utf-8",
    )
    try:
        v = check_font_sizes(svg)
        assert not any(x.get("severity") == "error" for x in v)
    finally:
        svg.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def test_check_no_hex_literals_pytest():
    theme = _build_dark_frost()
    svg = _make_temp_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '  <rect x="0" y="0" width="100" height="100" fill="#1A1A1A"/>'
        '</svg>'
    )
    try:
        v = check_no_hex_literals(svg, theme=theme)
        assert v, "expected #1A1A1A to be flagged"
        assert v[0]["color"] == "#1A1A1A"
    finally:
        svg.unlink(missing_ok=True)


def test_check_no_hex_literals_token_allowed_pytest():
    theme = _build_dark_frost()
    svg = _make_temp_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '  <rect x="0" y="0" width="100" height="100" fill="#0A0A0A"/>'
        '</svg>'
    )
    try:
        v = check_no_hex_literals(svg, theme=theme)
        assert v == [], f"theme tokens should be allowed; got: {v}"
    finally:
        svg.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
