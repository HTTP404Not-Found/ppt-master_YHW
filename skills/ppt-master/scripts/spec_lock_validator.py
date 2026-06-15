#!/usr/bin/env python3
"""
PPT Master - spec_lock.md Theme Validator

Validates that a project spec_lock.md contains the required ## Theme section
with the mandatory fields introduced by Strategist's 9th confirmation
(strategist.md §1.i Theme Selection). Outputs PASS when valid, lists missing
fields otherwise. Exits non-zero on failure so it can gate downstream steps.

Usage:
    python3 scripts/spec_lock_validator.py <project>/spec_lock.md
    python3 scripts/spec_lock_validator.py projects/my-project/spec_lock.md

Examples:
    python3 scripts/spec_lock_validator.py examples/ppt169_glassmorphism_demo/spec_lock.md
    python3 scripts/spec_lock_validator.py path/to/spec_lock.md --strict

Exit codes:
    0 - PASS (all required Theme fields present)
    1 - FAIL (missing required Theme fields, malformed section, or file error)
    2 - INVALID USAGE (no path argument, file not found, etc.)

Dependencies:
    None (only uses standard library)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Required top-level fields under ## Theme. kind, applied_to_pages, and the
# token reference block are also enforced via structural rules below; these
# five keys are the schema fields Strategist must set during the 9th
# confirmation.
REQUIRED_THEME_FIELDS: tuple[str, ...] = (
    'theme_id',
    'kind',
    'applied_to_pages',
    'min_text_ratio',
    'min_stroke_ratio',
)

# Theme IDs permitted by the four-theme catalog (strategist.md §1.i).
# Adding a new theme requires updating strategist.md AND this set.
ALLOWED_THEME_IDS: frozenset[str] = frozenset({
    'dark-frost',
    'dark-warm',
    'light-snow',
    'light-cream',
})

ALLOWED_THEME_KINDS: frozenset[str] = frozenset({'dark', 'light'})

# Required token keys for a complete theme. Keep in sync with the
# executor-base.md §8.1 token reference block.
REQUIRED_TOKEN_KEYS: tuple[str, ...] = (
    'bg-canvas',
    'bg-surface',
    'text-primary',
    'text-secondary',
    'text-muted',
    'accent',
    'accent-warm',
    'stroke-frame',
    'stroke-divider',
    'fill-card',
)

# Regex that captures the body of a markdown section between two H2 headings.
# We split on ^## lines (any level-2 markdown heading) so we can isolate
# the Theme block from the rest of the document.
SECTION_SPLIT_RE = re.compile(r'(?m)^##\s+')


def extract_theme_section(text: str) -> str | None:
    """Return the markdown body under the first `## Theme` heading, or None
    if no such heading exists. The returned text runs until the next H2
    heading or end of file."""
    lines = text.splitlines(keepends=True)
    in_theme = False
    captured: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('## '):
            heading = stripped[3:].strip()
            if heading.lower() == 'theme':
                in_theme = True
                continue
            if in_theme:
                # Reached the next ## section — stop.
                break
        if in_theme:
            captured.append(line)
    if not in_theme:
        return None
    return ''.join(captured)


def parse_field_table(body: str) -> dict[str, str]:
    """Parse a `| Field | Value |` style table inside the Theme section body.
    Returns a dict of lowercased field name -> value. Only the first row per
    field wins — duplicates are ignored, with a follow-up warning if needed."""
    fields: dict[str, str] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line.startswith('|'):
            continue
        # Skip the markdown table separator row (| --- | --- |).
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) < 2:
            continue
        if all(re.fullmatch(r'[-:]+', cell) for cell in cells):
            continue
        key = cells[0].strip().lower()
        value = cells[1].strip()
        if key and value and key not in fields:
            fields[key] = value
    return fields


def parse_token_block(body: str) -> dict[str, str]:
    """Parse the `### Tokens 參考` block as `- key: value` lines."""
    tokens: dict[str, str] = {}
    in_block = False
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line.startswith('### '):
            in_block = 'token' in line.lower() or 'token' in line
            continue
        if not in_block:
            continue
        if not line.startswith('- '):
            continue
        payload = line[2:]
        if ':' not in payload:
            continue
        key, value = payload.split(':', 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value and key not in tokens:
            tokens[key] = value
    return tokens


def validate(spec_lock_path: Path) -> tuple[bool, list[str]]:
    """Run all validation checks. Returns (passed, messages). On pass,
    messages is empty. On fail, messages lists every issue found (the CLI
    prints them as a numbered list)."""
    if not spec_lock_path.exists():
        return False, [f'File not found: {spec_lock_path}']
    if not spec_lock_path.is_file():
        return False, [f'Not a file: {spec_lock_path}']

    try:
        text = spec_lock_path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return False, [f'File is not valid UTF-8: {spec_lock_path}']

    theme_body = extract_theme_section(text)
    if theme_body is None:
        return False, [
            'Missing required section: ## Theme',
            'Add a `## Theme` block at the end of spec_lock.md with theme_id, kind, '
            'applied_to_pages, min_text_ratio, min_stroke_ratio, and a Tokens 參考 '
            'subsection (see strategist.md §1.i.1 for the canonical template).',
        ]

    issues: list[str] = []

    fields = parse_field_table(theme_body)
    for required_key in REQUIRED_THEME_FIELDS:
        if required_key not in fields:
            issues.append(
                f'Missing required field in ## Theme table: `{required_key}`'
            )

    theme_id = fields.get('theme_id', '').strip()
    if theme_id and theme_id not in ALLOWED_THEME_IDS:
        issues.append(
            f"Invalid theme_id `{theme_id}`. Allowed: "
            f"{', '.join(sorted(ALLOWED_THEME_IDS))} (see strategist.md §1.i catalog)."
        )

    kind = fields.get('kind', '').strip()
    if kind and kind not in ALLOWED_THEME_KINDS:
        issues.append(
            f"Invalid kind `{kind}`. Allowed: dark, light."
        )

    # Cross-check kind against theme_id prefix.
    if theme_id and kind:
        expected_kind_prefix = theme_id.split('-', 1)[0]
        if expected_kind_prefix != kind:
            issues.append(
                f"kind `{kind}` does not match theme_id `{theme_id}` "
                f"(expected `{expected_kind_prefix}`)."
            )

    tokens = parse_token_block(theme_body)
    if not tokens:
        issues.append(
            'Missing `### Tokens 參考` subsection under ## Theme '
            '(or the subsection has no `- key: value` lines).'
        )
    else:
        for required_token in REQUIRED_TOKEN_KEYS:
            if required_token not in tokens:
                issues.append(
                    f'Missing required token in `### Tokens 參考`: `{required_token}`'
                )

    return (len(issues) == 0), issues


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Validate that a project spec_lock.md contains the '
                    'required ## Theme section (Strategist 9th confirmation).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Exit codes:\n'
            '  0  PASS - all required Theme fields present\n'
            '  1  FAIL - one or more required Theme fields missing\n'
            '  2  Invalid usage (file not found, missing path, etc.)\n'
        ),
    )
    parser.add_argument(
        'spec_lock_path',
        help='Path to the project spec_lock.md file',
    )
    parser.add_argument(
        '--strict',
        action='store_true',
        help='Reserved for future use; currently behaves the same as default',
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    spec_lock_path = Path(args.spec_lock_path)

    if not spec_lock_path.exists():
        print(f'Error: file not found: {spec_lock_path}', file=sys.stderr)
        return 2

    passed, issues = validate(spec_lock_path)

    if passed:
        print('PASS: spec_lock.md ## Theme section is valid.')
        print('  - All required schema fields present')
        print('  - theme_id matches the four-theme catalog')
        print('  - kind matches theme_id prefix')
        print('  - Tokens 參考 block contains every required token')
        return 0

    print('FAIL: spec_lock.md ## Theme section is missing required content:',
          file=sys.stderr)
    for i, issue in enumerate(issues, 1):
        print(f'  [{i}] {issue}', file=sys.stderr)
    print(
        '\nFix: append a ## Theme block to spec_lock.md using the template '
        'in strategist.md §1.i.1 (Nine Confirmations → i. Theme Selection).',
        file=sys.stderr,
    )
    return 1


if __name__ == '__main__':
    raise SystemExit(main())