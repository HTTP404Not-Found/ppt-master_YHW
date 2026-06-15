#!/usr/bin/env python3
"""
PPT Master - SVG Post-processing Tool (Unified Entry Point)

Processes SVG files from svg_output/ and outputs them to svg_final/.
By default, all processing steps are executed. You can also specify
individual steps via arguments.

Architecture note: this module's outputs feed svg_final/ on disk AND its
sub-modules (svg_finalize.embed_icons, svg_finalize.flatten_tspan, ...)
are memory-reused by svg_to_pptx during native conversion. Deleting any
step here may also break native pptx output, not just svg_final/.
See docs/technical-design.md "Post-Processing Pipeline" before modifying.

Usage:
    # Execute all processing steps (recommended)
    python3 scripts/finalize_svg.py <project_directory>

    # Execute only specific steps
    python3 scripts/finalize_svg.py <project_directory> --only embed-icons fix-rounded

Examples:
    python3 scripts/finalize_svg.py projects/my_project
    python3 scripts/finalize_svg.py examples/ppt169_demo --only embed-icons

Processing options:
    embed-icons   - Replace <use data-icon="..."/> with actual icon SVG
    align-images  - Align (slice/meet) and Base64-embed all <image> in one pass.
                    Replaces the former crop-images + fix-aspect + embed-images
                    trio. The old names remain accepted as aliases for the
                    merged step, so existing --only invocations keep working.
    flatten-text  - Convert <tspan> to independent <text> (for special renderers)
    fix-rounded   - Convert <rect rx="..."/> to <path> (for PPT shape conversion)
"""

import os
import sys
import shutil
import argparse
import json
import re
from pathlib import Path

# Import finalize helpers from the internal package.
sys.path.insert(0, str(Path(__file__).parent))
from svg_finalize.align_embed_images import (
    align_and_embed_images_in_svg,
    count_office_vector_refs_in_svg,
)
from svg_finalize.embed_icons import process_svg_file as embed_icons_in_file

# Optional integration: token expansion runs *before* any other post-process
# so the downstream steps operate on the resolved colour literals, not on
# ``var(--token-name)`` strings. Missing import is degraded gracefully —
# finalize still works without token expansion (backward compatibility).
try:
    import contrast_checker as _contrast_checker
except ImportError:
    _contrast_checker = None


def safe_print(text: str) -> None:
    """Print text while tolerating Windows terminal encoding limits."""
    try:
        print(text)
    except UnicodeEncodeError:
        replacements = {
            chr(0x23F3): "[..]",
            chr(0x2705): "[DONE]",
            chr(0x274C): "[ERROR]",
            chr(0x26A0) + chr(0xFE0F): "[WARN]",
            chr(0x1F4C1): "[DIR]",
            chr(0x1F4C4): "[FILE]",
            chr(0x1F4E6): "[OK]",
        }
        for source, target in replacements.items():
            text = text.replace(source, target)
        print(text)


def process_flatten_text(svg_file: Path, verbose: bool = False) -> bool:
    """Flatten text in a single SVG file (in-place modification)"""
    try:
        from svg_finalize.flatten_tspan import flatten_text_with_tspans
        from xml.etree import ElementTree as ET

        tree = ET.parse(str(svg_file))
        changed = flatten_text_with_tspans(tree)

        if changed:
            tree.write(str(svg_file), encoding='unicode', xml_declaration=False)
            if verbose:
                safe_print(f"   [OK] {svg_file.name}: text flattened")
        return changed
    except Exception as e:
        if verbose:
            safe_print(f"   [ERROR] {svg_file.name}: {e}")
        return False


def process_rounded_rect(svg_file: Path, verbose: bool = False) -> int:
    """Convert rounded rectangles in a single SVG file (in-place modification)"""
    try:
        from svg_finalize.svg_rect_to_path import process_svg

        with open(svg_file, 'r', encoding='utf-8') as f:
            content = f.read()

        processed, count = process_svg(content, verbose=False)

        if count > 0:
            with open(svg_file, 'w', encoding='utf-8') as f:
                f.write(processed)
            if verbose:
                safe_print(f"   [OK] {svg_file.name}: {count} rounded rectangle(s)")
        return count
    except Exception as e:
        if verbose:
            safe_print(f"   [ERROR] {svg_file.name}: {e}")
        return 0


def _load_theme_from_spec_lock(spec_lock_path: Path) -> dict | None:
    """Parse ``## Theme`` section of ``spec_lock.md`` into a theme dict.

    Expected shape under ``## Theme``::

        - tokens: path/to/theme.json      (preferred — external theme file)
        - tokens: '{"bg-canvas": "#0A0A0A", …}'   (inline JSON, fallback)
        - bg: #0A0A0A                     (optional page bg override)

    Returns ``{}`` when the section is absent (backward compat — legacy
    decks have no Theme and must keep working) and ``None`` when the file
    is unreadable.
    """
    if not spec_lock_path.exists():
        return None
    try:
        text = spec_lock_path.read_text(encoding="utf-8")
    except OSError:
        return None

    in_theme = False
    theme: dict = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            in_theme = line[3:].strip().lower() == "theme"
            continue
        if not in_theme:
            continue
        m = re.match(r"^-\s+tokens\s*:\s*(.+?)\s*$", line)
        if m:
            value = m.group(1).strip().strip('"').strip("'")
            # Either a path to a JSON file or inline JSON.
            if value.startswith("{"):
                try:
                    theme["tokens"] = json.loads(value)
                except json.JSONDecodeError:
                    pass
            else:
                # Resolve relative to project_dir (spec_lock lives at
                # project root, theme JSON usually lives next to it).
                theme_path = (spec_lock_path.parent / value).resolve()
                if theme_path.exists():
                    try:
                        theme = json.loads(theme_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        theme = {"tokens": {}}
                else:
                    # Theme file referenced but missing — log via the
                    # warnings list. Keep theme empty so the rest of the
                    # pipeline keeps working.
                    theme = {"tokens": {}}
            continue
        m = re.match(r"^-\s+bg\s*:\s*(#[0-9A-Fa-f]{3,8})\s*$", line)
        if m:
            theme["bg"] = m.group(1).upper()
    return theme


def process_token_expansion(svg_file: Path, theme: dict,
                             verbose: bool = False) -> tuple[int, list[str]]:
    """Replace ``var(--token)`` with the matching theme hex in ``svg_file``.

    Returns ``(expanded_count, warnings)``. The function is a no-op when
    ``theme`` has no ``tokens`` mapping, so legacy decks without a Theme
    pass through unchanged (backward compatibility).
    """
    if _contrast_checker is None or not theme or not theme.get("tokens"):
        return 0, []

    try:
        content = svg_file.read_text(encoding="utf-8")
    except OSError as e:
        return 0, [f"cannot read {svg_file.name}: {e}"]

    if "var(--" not in content:
        return 0, []

    new_content, warnings = _contrast_checker.expand_tokens(content, theme)
    if new_content != content:
        svg_file.write_text(new_content, encoding="utf-8")
    # Count the number of ``var(--…)`` occurrences that were replaced.
    n = len(re.findall(r"var\(\s*--[A-Za-z0-9_-]+\s*\)", content))
    return n, warnings


def finalize_project(
    project_dir: Path,
    options: dict[str, bool],
    dry_run: bool = False,
    quiet: bool = False,
    compress: bool = False,
    max_dimension: int | None = None,
) -> bool:
    """
    Finalize SVG files in the project

    Args:
        project_dir: Project directory path
        options: Processing options dictionary
        dry_run: Preview only, do not execute
        quiet: Quiet mode, reduce output
        compress: Compress images before embedding
        max_dimension: Downscale images exceeding this dimension
    """
    svg_output = project_dir / 'svg_output'
    svg_final = project_dir / 'svg_final'
    icons_dir = Path(__file__).parent.parent / 'templates' / 'icons'

    # Check if svg_output exists
    if not svg_output.exists():
        safe_print(f"[ERROR] svg_output directory not found: {svg_output}")
        return False

    # Get list of SVG files
    svg_files = list(svg_output.glob('*.svg'))
    if not svg_files:
        safe_print(f"[ERROR] No SVG files in svg_output")
        return False

    if not quiet:
        print()
        safe_print(f"[DIR] Project: {project_dir.name}")
        safe_print(f"[FILE] {len(svg_files)} SVG file(s)")

    if dry_run:
        safe_print("[PREVIEW] Preview mode, no operations will be executed")
        return True

    # Step 1: Copy directory
    if svg_final.exists():
        shutil.rmtree(svg_final)
    shutil.copytree(svg_output, svg_final)

    # Step 1a: Token expansion (when spec_lock.md carries a ## Theme
    # section). Runs before every other step so downstream stages see the
    # resolved hex literals, not var(--token) placeholders. No Theme
    # section → no-op (backward compat with legacy decks).
    spec_lock_path = project_dir / "spec_lock.md"
    theme = _load_theme_from_spec_lock(spec_lock_path) or {}
    if theme.get("tokens"):
        token_count = 0
        token_warnings: list[str] = []
        for svg_file in svg_final.glob("*.svg"):
            n, warnings = process_token_expansion(svg_file, theme, verbose=False)
            token_count += n
            token_warnings.extend(warnings)
        if not quiet:
            if token_count > 0:
                safe_print(f"[1a] Token expansion: {token_count} placeholder(s) resolved")
                for w in token_warnings:
                    safe_print(f"   [WARN] {w}")
        # After expansion, surface a "token coverage incomplete" warning if
        # the spec_lock Theme is active but the SVGs still contain raw hex
        # literals that aren't in the theme's token table. The audit uses
        # contrast_checker.check_no_hex_literals (optional integration).
        if _contrast_checker is not None and not quiet:
            token_hexes = set()
            for v in (theme.get("tokens") or {}).values():
                if isinstance(v, str) and v.startswith("#"):
                    token_hexes.add(v.upper())
                elif isinstance(v, dict):
                    for vv in v.values():
                        if isinstance(vv, str) and vv.startswith("#"):
                            token_hexes.add(vv.upper())
            files_with_hex = 0
            for svg_file in svg_final.glob("*.svg"):
                violations = _contrast_checker.check_no_hex_literals(
                    svg_file, theme=theme,
                )
                if violations:
                    files_with_hex += 1
            if files_with_hex:
                safe_print(
                    f"[WARN] Token coverage incomplete: "
                    f"{files_with_hex} file(s) still contain hex literals "
                    "not in the theme token table."
                )

    if not quiet:
        print()

    # Step 2: Embed icons
    if options.get('embed_icons'):
        if not quiet:
            safe_print("[1/4] Embedding icons...")
        icons_count = 0
        for svg_file in svg_final.glob('*.svg'):
            count = embed_icons_in_file(svg_file, icons_dir, dry_run=False, verbose=False)
            icons_count += count
        if not quiet:
            if icons_count > 0:
                safe_print(f"      {icons_count} icon(s) embedded")
            else:
                safe_print("      No icons")

    # Step 3: Align (slice/meet) and Base64-embed all <image> in one pass.
    # Replaces the former crop-images / fix-aspect / embed-images trio: the
    # spatial transform (slice → crop, meet → fit-box) and the asset embed
    # are mutually exclusive branches per image, sequenced together so each
    # SVG is only parsed and serialized once and each bitmap is only read
    # from disk once.
    if options.get('align_images'):
        if not quiet:
            safe_print("[2/4] Aligning + embedding images...")
        img_count = 0
        img_errors = 0
        office_vector_count = 0
        for svg_file in svg_final.glob('*.svg'):
            office_vector_count += count_office_vector_refs_in_svg(svg_file)
            count, errs = align_and_embed_images_in_svg(
                svg_file,
                dry_run=False,
                verbose=False,
                compress=compress,
                max_dimension=max_dimension,
            )
            img_count += count
            img_errors += errs
        if not quiet:
            if img_count > 0:
                msg = f"      {img_count} image(s) aligned + embedded"
                if img_errors:
                    msg += f"  ({img_errors} error(s))"
                safe_print(msg)
                if office_vector_count:
                    safe_print(
                        f"      {office_vector_count} Office vector(s) left external "
                        "for native PPTX passthrough"
                    )
            elif office_vector_count:
                safe_print(
                    f"      {office_vector_count} Office vector(s) left external "
                    "for native PPTX passthrough"
                )
            else:
                safe_print("      No images")

    # Step 4: Flatten text
    if options.get('flatten_text'):
        if not quiet:
            safe_print("[3/4] Flattening text...")
        flatten_count = 0
        for svg_file in svg_final.glob('*.svg'):
            if process_flatten_text(svg_file, verbose=False):
                flatten_count += 1
        if not quiet:
            if flatten_count > 0:
                safe_print(f"      {flatten_count} file(s) processed")
            else:
                safe_print("      No processing needed")

    # Step 5: Convert rounded rects to Path
    if options.get('fix_rounded'):
        if not quiet:
            safe_print("[4/4] Converting rounded rects to Path...")
        rounded_count = 0
        for svg_file in svg_final.glob('*.svg'):
            count = process_rounded_rect(svg_file, verbose=False)
            rounded_count += count
        if not quiet:
            if rounded_count > 0:
                safe_print(f"      {rounded_count} rounded rectangle(s) converted")
            else:
                safe_print("      No rounded rectangles")

    # Done
    if not quiet:
        print()
        safe_print("[OK] Done!")
        print()
        print("Next steps:")
        print(f"  python scripts/svg_to_pptx.py \"{project_dir}\"")

    return True


def main() -> None:
    """Run the CLI entry point."""
    parser = argparse.ArgumentParser(
        description='PPT Master - SVG Post-processing Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s projects/my_project           # Execute all processing (default)
  %(prog)s projects/my_project --only embed-icons fix-rounded
  %(prog)s projects/my_project -q        # Quiet mode

Processing options (for --only):
  embed-icons   Embed icons
  align-images  Align (slice/meet) + Base64-embed all <image> (single pass)
  flatten-text  Flatten text
  fix-rounded   Convert rounded rects to Path

Aliases (still accepted):
  crop-images, fix-aspect, embed-images  → all map to align-images
        '''
    )

    parser.add_argument('project_dir', type=Path, help='Project directory path')
    parser.add_argument(
        '--only', nargs='+', metavar='OPTION',
        choices=[
            'embed-icons',
            'align-images',
            # Backwards-compatible aliases — all three map to align-images now.
            'crop-images', 'fix-aspect', 'embed-images',
            'flatten-text', 'fix-rounded',
        ],
        help=('Execute only specified processing steps (default: all). '
              'crop-images / fix-aspect / embed-images are accepted as '
              'aliases for the merged align-images step.'),
    )
    parser.add_argument('--dry-run', '-n', action='store_true',
                        help='Preview only, do not execute')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='Quiet mode, reduce output')
    parser.add_argument('--compress', action='store_true',
                        help='Compress images before embedding (JPEG quality=85, PNG optimize)')
    parser.add_argument('--max-dimension', type=int, default=None,
                        help='Downscale images exceeding this dimension on either axis (e.g., 2560)')

    args = parser.parse_args()

    if not args.project_dir.exists():
        safe_print(f"[ERROR] Project directory does not exist: {args.project_dir}")
        sys.exit(1)

    # Aliases: any of crop-images / fix-aspect / embed-images implies the
    # merged align-images step. Older invocations stay valid.
    _ALIGN_ALIASES = {'align-images', 'crop-images', 'fix-aspect', 'embed-images'}

    # Determine processing options
    if args.only:
        only = set(args.only)
        options = {
            'embed_icons': 'embed-icons' in only,
            'align_images': bool(only & _ALIGN_ALIASES),
            'flatten_text': 'flatten-text' in only,
            'fix_rounded': 'fix-rounded' in only,
        }
    else:
        # Execute all by default
        options = {
            'embed_icons': True,
            'align_images': True,
            'flatten_text': True,
            'fix_rounded': True,
        }

    success = finalize_project(args.project_dir, options, args.dry_run, args.quiet,
                               compress=args.compress,
                               max_dimension=args.max_dimension)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
