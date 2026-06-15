"""Regression tests for the in-memory <use data-icon="..."/> expansion path
inside ``svg_to_pptx``.

Background
----------
The native-mode SVG -> DrawingML converter does an in-memory expansion of
``<use data-icon="lib/name">`` placeholders so it can consume ``svg_output/``
directly (see ``svg_to_pptx/use_expander.py``). When a placeholder refers
to an icon that does not exist in the icon library (or has malformed
attributes), the placeholder survives expansion. Before the fix, that
leftover triggered an ``SvgNativeConversionError`` from
``_collect_unsupported_visuals`` and crashed the entire export on a
single bad icon. The fix strips unresolvable placeholders after expansion
and emits a clear per-icon warning instead.

Run via ``python -m pytest scripts/tests/test_svg_to_pptx_smoke.py``
(preferred) or ``python -m unittest scripts.tests.test_svg_to_pptx_smoke``
(stdlib fallback). No third-party deps.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

# Ensure scripts/ is on sys.path regardless of invocation directory.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from svg_to_pptx.use_expander import (  # noqa: E402
    expand_use_data_icons,
    strip_unresolved_use_data_icons,
)

# Minimal SVG envelope; the icon library's namespace prefix matches what
# ``expand_use_data_icons`` parses via ET.
SVG_NS = "http://www.w3.org/2000/svg"
SVG_HEADER = (
    '<svg xmlns="{ns}" viewBox="0 0 1280 720" width="1280" height="720">'
    '<g id="root">{{body}}</g></svg>'
).format(ns=SVG_NS)


def _parse_svg(body: str) -> ET.Element:
    """Parse a tiny SVG envelope and return its root element."""
    return ET.fromstring(SVG_HEADER.format(body=body))


def _local_tag(elem: ET.Element) -> str:
    return elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag


def _serialize(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode")


def _count_unresolved(root: ET.Element) -> list[str]:
    """Return the list of unresolved ``data-icon`` attribute values in *root*."""
    return [
        e.get("data-icon", "")
        for e in root.iter()
        if _local_tag(e) == "use" and e.get("data-icon")
    ]


class ExpandUseDataIconsTests(unittest.TestCase):
    """Behaviour of ``expand_use_data_icons`` against an empty/missing library."""

    def setUp(self) -> None:
        # Always use a non-existent icons dir so the expander cannot resolve
        # anything; this isolates the strip step from the expand step.
        self.fake_icons_dir = Path(tempfile.mkdtemp()) / "icons-does-not-exist"
        self.assertFalse(self.fake_icons_dir.exists())

    def test_missing_icons_dir_leaves_placeholders_untouched(self) -> None:
        root = _parse_svg(
            '<use data-icon="phosphor-duotone/gauge" x="0" y="0" '
            'width="48" height="48" fill="#3DDDFC"/>'
        )
        expanded = expand_use_data_icons(root, self.fake_icons_dir)
        self.assertEqual(expanded, 0)
        self.assertEqual(len(_count_unresolved(root)), 1)


class StripUnresolvedUseDataIconsTests(unittest.TestCase):
    """The regression: leftover placeholders must be stripped, not crash."""

    def test_strips_single_unresolved_placeholder(self) -> None:
        root = _parse_svg(
            '<use data-icon="phosphor-duotone/gauge" x="0" y="0" '
            'width="48" height="48" fill="#3DDDFC"/>'
        )
        unresolved = strip_unresolved_use_data_icons(root)
        self.assertEqual(unresolved, ["phosphor-duotone/gauge"])
        self.assertEqual(_count_unresolved(root), [])
        # Surrounding geometry is untouched.
        self.assertIn('id="root"', _serialize(root))

    def test_strips_multiple_unresolved_placeholders(self) -> None:
        body = (
            '<use data-icon="phosphor-duotone/gauge" x="0" y="0" '
            'width="48" height="48" fill="#3DDDFC"/>'
            '<use data-icon="phosphor-duotone/coin" x="60" y="0" '
            'width="48" height="48" fill="#3DDDFC"/>'
            '<use data-icon="phosphor-duotone/robot" x="120" y="0" '
            'width="48" height="48" fill="#3DDDFC"/>'
        )
        root = _parse_svg(body)
        unresolved = strip_unresolved_use_data_icons(root)
        self.assertEqual(
            sorted(unresolved),
            sorted([
                "phosphor-duotone/gauge",
                "phosphor-duotone/coin",
                "phosphor-duotone/robot",
            ]),
        )
        self.assertEqual(_count_unresolved(root), [])

    def test_does_not_touch_non_data_icon_use(self) -> None:
        """A ``<use>`` element without a ``data-icon`` attribute is left
        alone. The strip helper only targets the project-internal
        placeholder, not the SVG-standard ``<use>`` element."""
        root = _parse_svg(
            '<use href="#real-shape" x="0" y="0" width="48" height="48"/>'
        )
        unresolved = strip_unresolved_use_data_icons(root)
        self.assertEqual(unresolved, [])
        # The href-style use is still present.
        self.assertIn('href="#real-shape"', _serialize(root))

    def test_expand_then_strip_yields_clean_tree(self) -> None:
        """End-to-end: with no icons dir, expand is a no-op and strip
        removes the placeholder so the unsupported-element check downstream
        sees nothing."""
        root = _parse_svg(
            '<use data-icon="phosphor-duotone/gauge" x="0" y="0" '
            'width="48" height="48" fill="#3DDDFC"/>'
        )
        # Use a guaranteed-missing icons dir so expand is a no-op.
        fake_icons = Path(tempfile.mkdtemp()) / "icons-does-not-exist"
        expanded = expand_use_data_icons(root, fake_icons)
        unresolved = strip_unresolved_use_data_icons(root)
        self.assertEqual(expanded, 0)
        self.assertEqual(unresolved, ["phosphor-duotone/gauge"])
        self.assertEqual(_count_unresolved(root), [])


class GlassmorphismReproTest(unittest.TestCase):
    """Pin the exact scenario that triggered the original bug report.

    The glassmorphism example references ``phosphor-duotone/gauge`` on
    slide 3, an icon that does not exist in the icon library. Before the
    fix, this triggered::

        svg_to_pptx.drawingml_converter.SvgNativeConversionError:
            03_three_pains.svg: unsupported visual SVG element(s):
            /svg[4]/g[5]/use

    After the fix, the same input is processable: the placeholder is
    stripped, the surrounding geometry is preserved, and the rest of the
    slide exports cleanly.
    """

    def setUp(self) -> None:
        self.fake_icons_dir = Path(tempfile.mkdtemp()) / "icons-does-not-exist"

    def test_three_pains_pillar_perf_placeholder_is_stripped(self) -> None:
        body = (
            # Replicates the relevant fragment of 03_three_pains.svg:
            # pillar-perf holds a circle plus the unresolved <use>.
            '<g id="pillar-perf">'
            '  <circle cx="160" cy="220" r="32" fill="#0A0E27"/>'
            '  <use data-icon="phosphor-duotone/gauge" x="136" y="196" '
            '   width="48" height="48" fill="#3DDDFC"/>'
            '</g>'
        )
        root = _parse_svg(body)

        # Walk the same expand-then-strip pipeline the converter uses.
        expanded = expand_use_data_icons(root, self.fake_icons_dir)
        unresolved = strip_unresolved_use_data_icons(root)

        self.assertEqual(expanded, 0)
        self.assertEqual(unresolved, ["phosphor-duotone/gauge"])

        # The leftover <use> would have been flagged by
        # _collect_unsupported_visuals; it must not be in the tree.
        leftover = [
            e for e in root.iter()
            if _local_tag(e) == "use" and e.get("data-icon")
        ]
        self.assertEqual(leftover, [], "leftover <use> would crash the converter")

        # Geometry that survived (the circle) is still present.
        self.assertIn('id="pillar-perf"', _serialize(root))
        self.assertIn('cx="160"', _serialize(root))


if __name__ == "__main__":
    unittest.main()