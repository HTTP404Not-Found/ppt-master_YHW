import sys
sys.path.insert(0, r'C:\ppt-master-main\ppt-master-main\skills\ppt-master\scripts')
from contrast_checker import compute_ratio
r1 = compute_ratio('#000000', '#FFFFFF')
r2 = compute_ratio('#0A0A0A', '#F5F5F5')
print(f'#000000 vs #FFFFFF: {r1:.4f}')
print(f'#0A0A0A vs #F5F5F5: {r2:.4f}')
expected_1 = 21.0
expected_2 = 17.4
print(f'Expected 21.0, got {r1:.4f}: PASS' if abs(r1-expected_1) < 0.01 else f'FAIL: expected {expected_1}, got {r1}')
print(f'Expected ~17.4 +/- 0.5, got {r2:.4f}: PASS' if abs(r2-expected_2) < 0.5 else f'FAIL: expected {expected_2}, got {r2}')
