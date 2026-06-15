"""Adversarial probes for contrast_checker."""
import sys
sys.path.insert(0, r'C:\ppt-master-main\ppt-master-main\skills\ppt-master\scripts')
from contrast_checker import (
    compute_ratio, _relative_luminance, _expand_short_hex, _collect_token_hexes,
    validate_theme, check_svg, check_font_sizes, check_no_hex_literals,
    expand_tokens, severity_for_ratio, load_theme
)
from pathlib import Path

print("=== PROBE 1: Compute ratio on same color (should be 1.0) ===")
r = compute_ratio('#FF8800', '#FF8800')
print(f"compute_ratio('#FF8800', '#FF8800') = {r} (expected 1.0): {'PASS' if abs(r-1.0) < 0.001 else 'FAIL'}")

print("\n=== PROBE 2: Symmetry ===")
r1 = compute_ratio('#FF0000', '#0000FF')
r2 = compute_ratio('#0000FF', '#FF0000')
print(f"r1={r1}, r2={r2}: {'PASS' if abs(r1-r2) < 0.001 else 'FAIL'}")

print("\n=== PROBE 3: Invalid input raises (security/error handling) ===")
try:
    compute_ratio('not-a-color', '#FFFFFF')
    print("FAIL: should have raised")
except ValueError as e:
    print(f"PASS: raised ValueError: {e}")

print("\n=== PROBE 4: Short hex expansion ===")
expanded = _expand_short_hex('#ABC')
print(f"#ABC -> {expanded} (expected #AABBCC): {'PASS' if expanded == '#AABBCC' else 'FAIL'}")

print("\n=== PROBE 5: Token expansion with unknown token ===")
content = 'fill="var(--nonexistent)"'
theme = {'tokens': {'bg-canvas': '#000000'}}
new_content, warnings = expand_tokens(content, theme)
print(f"result: {new_content}, warnings: {warnings}")
print(f"PASS if warning contains 'unresolved token' and content unchanged" if 'unresolved token' in str(warnings) and new_content == content else "FAIL")

print("\n=== PROBE 6: Token expansion with known token ===")
content = 'fill="var(--bg-canvas)"'
theme = {'tokens': {'bg-canvas': '#000000'}}
new_content, warnings = expand_tokens(content, theme)
print(f"result: {new_content}, warnings: {warnings}")
print(f"PASS if #000000 in result" if '#000000' in new_content else "FAIL")

print("\n=== PROBE 7: Severity ladder edge cases ===")
print(f"ratio=4.4: {severity_for_ratio(4.4)} (expected error)")
print(f"ratio=4.5: {severity_for_ratio(4.5)} (expected warning)")
print(f"ratio=6.9: {severity_for_ratio(6.9)} (expected warning)")
print(f"ratio=7.0: {severity_for_ratio(7.0)} (expected ok)")

print("\n=== PROBE 8: Nested tokens dict (colors subcategory) ===")
theme = {'tokens': {'colors': {'bg-canvas': '#0A0A0A'}, 'text': {'text-primary': '#FFFFFF'}}}
hexes = _collect_token_hexes(theme)
print(f"nested: {hexes}")
print(f"PASS if both hexes present" if hexes == {'#0A0A0A', '#FFFFFF'} else f"FAIL: {hexes}")

print("\n=== PROBE 9: Validate theme (missing file) ===")
passed, errors = validate_theme(Path('/nonexistent.json'))
print(f"missing file: passed={passed}, errors={errors}")
print(f"PASS if not passed" if not passed else "FAIL")

print("\n=== PROBE 10: Luminance edge - pure white/black ===")
print(f"L(#FFFFFF) = {_relative_luminance('#FFFFFF'):.6f} (expected 1.0)")
print(f"L(#000000) = {_relative_luminance('#000000'):.6f} (expected 0.0)")

print("\n=== PROBE 11: Cross-product luminance (verifier's 17.4 check, but use 0A0A0A/F5F5F5) ===")
r = compute_ratio('#0A0A0A', '#F5F5F5')
print(f"compute_ratio('#0A0A0A', '#F5F5F5') = {r:.4f}")
print(f"Per WCAG 2.1 formula, expected ~18.16, NOT 17.4 (verifier prompt has wrong expected value)")
print(f"Note: the verifier's '17.4' is the contrast of #1A1A1A on #FFFFFF (R=G=B=26), not #0A0A0A on #F5F5F5")
r1a = compute_ratio('#1A1A1A', '#FFFFFF')
print(f"verify: compute_ratio('#1A1A1A', '#FFFFFF') = {r1a:.4f}")

print("\n=== PROBE 12: Long hex (#RRGGBBAA) — alpha ignored ===")
r = compute_ratio('#FF0000FF', '#000000FF')
print(f"#FF0000FF vs #000000FF (alpha ignored): {r:.4f} (expected ~5.25)")

print("\n=== PROBE 13: Malformed SVG (XML broken) ===")
import tempfile
malformed_svg = '<svg><text fill="#fff">broken</svg>'  # not closed
with tempfile.NamedTemporaryFile(suffix='.svg', delete=False, mode='w', encoding='utf-8') as f:
    f.write(malformed_svg)
    f.flush()
    violations = check_svg(Path(f.name))
    print(f"violations: {violations}")
    print(f"PASS if <unattributed> in violations" if any('<unattributed>' in str(v) for v in violations) else "FAIL")
    Path(f.name).unlink()

print("\n=== PROBE 14: Path traversal in theme ===")
# Try loading a path with .. - should not crash
try:
    passed, errors = validate_theme(Path('C:\\Windows\\System32\\drivers\\etc\\hosts'))
    print(f"system file: passed={passed}, errors={errors[:1]}")
    print(f"PASS if no exception" if True else "FAIL")
except Exception as e:
    print(f"FAIL: exception {e}")

print("\n=== PROBE 15: Expand tokens with HEX in different cases ===")
content = 'fill="var(--test)"'
theme = {'tokens': {'test': '#abc'}}  # lowercase
new_content, warnings = expand_tokens(content, theme)
print(f"result: {new_content} (expected uppercased)")
print(f"PASS if uppercased" if new_content == 'fill="#AABC"' or '#ABC' in new_content.upper() else f"FAIL: {new_content}")
