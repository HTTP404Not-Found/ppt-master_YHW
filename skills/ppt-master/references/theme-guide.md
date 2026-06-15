# Theme Guide

> Companion to `skills/ppt-master/themes/`. Read this before picking a
> theme for a project, before editing a theme JSON, or before writing SVG
> that consumes theme tokens.

A *theme* is a semantic colour bundle — `bg-canvas`, `text-primary`,
`accent`, and so on — that the rest of the pipeline (Strategist, Executor,
`svg_quality_checker`, `finalize_svg`, `svg_to_pptx`) reasons about in
token terms instead of raw hex. The four shipped themes
(`dark-frost`, `dark-warm`, `light-snow`, `light-cream`) are the
authoritative seed set; new themes follow the same shape and pass
through `scripts/validate_theme.py` before they ship.

---

## 1. Why a Theme System (tokens, not hex)

**The problem with hex literals.** A typical deck has 50–200 SVG
elements, each of which sets `fill="#3A8DFF"` and `stroke="#1B4F8A"`.
Three months later, when brand refreshes blue to teal, every one of those
literals has to be hunted down by hand — and the analyst inevitably
misses one. Worse, "blue" is ambiguous: the body link `#3A8DFF` and the
chart series-2 `#3A8DFF` may *happen* to share a hex but mean different
things.

**The token solution.** Every colour is named by *role* (`accent`,
`text-muted`, `stroke-divider`). SVG fills reference the role
(`var(--accent)` or post-finalize the resolved hex), and the theme JSON
is the single source of truth. Brand refresh = edit four theme JSONs,
re-run finalize_svg, done.

Concrete wins:

- **WCAG math is done once.** Each theme's `ratios` block lists every
  pre-computed contrast ratio against `bg-canvas`. Quality-check tooling
  compares live SVG fills against that block instead of re-deriving the
  threshold per file.
- **Dark/light swap is one line.** `<spec_lock>` flips `theme_id` from
  `dark-frost` to `light-snow`; finalize_svg swaps the token table.
- **No hard-coded brand drift.** A contributor cannot quietly use
  `#3A8DFF` because the validator (`contrast_checker.py`,
  `check_no_hex_literals`) flags it.
- **Accessibility is reviewable.** Anyone reading the theme JSON can
  audit the whole palette in 30 seconds without opening SVGs.

---

## 2. The Four Seed Themes — Design Rationale

Each theme targets a different *content mood*. Choose by what the deck
is *saying*, not which looks prettiest on a colour card.

### 2.1 `dark-frost` — Default for tech / product

- **Mood:** Cool, analytical, cinematic. Feels like a product launch
  keynoted at 2am in a darkened room.
- **Palette:** `#0A0A0A` canvas, `#F5F5F5` near-white primary, `#5AC8FA`
  ice-blue accent, `#FF9F0A` amber for callouts.
- **Use when:** Software product launches, technical deep-dives, AI /
  data-platform pitches, anything in a dimmed room.
- **Avoid when:** Print, accessibility-first government / medical decks,
  long-form text-heavy reading.

### 2.2 `dark-warm` — Brand storytelling, lifestyle, premium hospitality

- **Mood:** Warm, hand-crafted, evening-light. Feels like a boutique
  hotel brand book.
- **Palette:** `#1A0F08` black-brown canvas, `#F8EFE4` cream primary,
  `#FFB347` amber accent, `#6FB4D8` muted teal as cool counterpoint.
- **Use when:** Hospitality, food & beverage, fashion, sustainability
  narratives, premium product reveals.
- **Avoid when:** Anything that needs to read as "clinical" or
  "data-dense" — warm biases perception toward editorial, not analytical.

### 2.3 `light-snow` — Default for consulting, dashboards, print

- **Mood:** Bright, neutral, business-formal. The default printed-report
  baseline.
- **Palette:** `#FFFFFF` canvas, `#1A1A1A` near-black primary, `#0A66C2`
  corporate blue accent, `#9A4A00` burnt orange for warning highlights.
- **Use when:** Consulting decks, financial reports, anything destined
  for print, dashboards that must read in bright daylight.
- **Avoid when:** Cinematic / immersive content where you want the
  canvas to recede.

### 2.4 `light-cream` — Editorial long-reads, heritage, paper-textured

- **Mood:** Vintage paper, editorial column, sustainability magazine.
  Reads like a printed zine.
- **Palette:** `#F5EFE3` parchment canvas, `#3A2E1F` deep brown primary,
  `#A8480F` burnt-sienna accent, `#8A5410` ochre warm accent.
- **Use when:** Editorial long-reads, heritage brand stories,
  sustainability reports, museum / cultural programming.
- **Avoid when:** Data-dense charts (cream biases contrast perception
  for chart series) or anything projecting on a warm-tinted screen.

### Decision tree

```
Is the deck projected in a dimmed room?
  ├── Yes → Is the brand editorial / lifestyle / premium?
  │     ├── Yes → dark-warm
  │     └── No  → dark-frost         (default dark)
  └── No  → Is the brand editorial / heritage / paper-textured?
        ├── Yes → light-cream
        └── No  → light-snow         (default light)
```

---

## 3. Picking Colours That Clear WCAG

The shipped themes all clear WCAG 2.1 AA by construction. If you are
extending or building a new theme, use this rubric.

### 3.1 Relative luminance (WCAG 2.x)

For each channel `c ∈ {R, G, B}` in `[0, 1]` (after dividing 8-bit hex
by 255):

```
c_lin = c / 12.92                       if c <= 0.03928
c_lin = ((c + 0.055) / 1.055) ** 2.4    otherwise
```

Relative luminance of the colour:

```
L = 0.2126 * R_lin + 0.7152 * G_lin + 0.0722 * B_lin
```

Contrast ratio between two colours:

```
ratio = (L_lighter + 0.05) / (L_darker + 0.05)
```

Reference checks baked into `validate_theme.py`:

| Pair                             | Floor |
|----------------------------------|-------|
| any `text-*` vs `bg-canvas`      | 4.5   |
| `stroke-frame` vs `bg-canvas`    | 3.0   |
| `accent` / `accent-warm` vs `bg` | 4.5 floor, **12.0 ceiling** |
| `stroke-divider` vs `bg-canvas`  | (decorative; no floor) |

### 3.2 Worked example — picking `text-primary` for `light-cream`

The canvas is `#F5EFE3`. To clear 4.5 we need a foreground with
luminance ≤ `L_bg / 5.5 - 0.05/5.5` ≈ `0.85 / 5.5` ≈ `0.155`.

We picked `#3A2E1F`:

```
R = 0x3A / 255 = 0.227 → ((0.227 + 0.055) / 1.055) ** 2.4 = 0.0418
G = 0x2E / 255 = 0.180 → ((0.180 + 0.055) / 1.055) ** 2.4 = 0.0262
B = 0x1F / 255 = 0.122 → ((0.122 + 0.055) / 1.055) ** 2.4 = 0.0128
L_fg = 0.2126*0.0418 + 0.7152*0.0262 + 0.0722*0.0128 = 0.0288
```

For `#F5EFE3` (canvas):

```
R = 245/255 = 0.961 → ((0.961+0.055)/1.055)**2.4 = 0.912
G = 239/255 = 0.937 → ((0.937+0.055)/1.055)**2.4 = 0.864
B = 227/255 = 0.890 → ((0.890+0.055)/1.055)**2.4 = 0.771
L_bg = 0.2126*0.912 + 0.7152*0.864 + 0.0722*0.771 = 0.874
```

Ratio = `(0.874 + 0.05) / (0.0288 + 0.05)` ≈ **11.5** — comfortably
above 4.5, well above AAA 7.0.

### 3.3 Quick-check rules of thumb

- **Dark theme text:** `#F5F5F5` / `#F8EFE4` / `#FFFFFF` all sit at
  L ≈ 0.90+ — anything that dark against `#0A0A0A` (L ≈ 0.003) clears
  17:1, no further tuning needed.
- **Light theme text:** Aim for primary L ≤ 0.02 against `#FFFFFF` (L =
  1.0). Pure black `#000000` clears 21:1; `#1A1A1A` clears 17.4:1;
  `#2A2A2A` still clears 14+. **Do not go lighter than `#4A4A4A`** for
  body — that's already at 8.86:1, AAA.
- **Light theme accent:** Bright accents fight the white field. Pick
  *deepened* hues: `#0A66C2` rather than `#5AC8FA`. The latter sits at
  L ≈ 0.50 — 3.06:1 against white, fails AA.

### 3.4 Anti-patterns

| Bad                                      | Why                                | Fix                          |
|------------------------------------------|------------------------------------|------------------------------|
| `accent: #FFFFFF` on dark canvas         | Glares at 21:1, no visual anchor  | Darken to L ≤ 0.20           |
| `accent: #5AC8FA` on white canvas        | 3:1, fails AA for non-text        | Use `#0A66C2` (5.69:1)       |
| `text-muted: #999999` on `#FFFFFF`       | 2.85:1, fails AA                   | Use `#707070` (4.95:1) or darker |
| Using raw hex inside SVG                 | Token coverage breaks; brand drift risk | Always `var(--token)` in SVG; finalize_svg expands |
| `text-primary` lighter than `text-secondary` | Breaks the ramp; reads wrong at a glance | text-primary must be the darkest (light) or lightest (dark) token |

---

## 4. Accent Saturation — Avoiding Glare

Pure-bright accents (`#FFFFFF`, `#00FFFF`, `#FFFF00`) hit 21:1 against
black. That looks like "high contrast" but the perceptual effect on a
projector is *glare* — the eye locks onto the bright pixel and loses
context. The reverse problem on light themes: a high-saturation
foreground against pure white vibrates, especially on cheap LCD panels.

**Heuristic:** accent-vs-bg ratio between **4.5:1 and 12:1**. The 12:1
ceiling is enforced by `validate_theme.py` as a *warning-grade*
constraint (not WCAG-required, but a perceptual guardrail).

| Theme       | Accent hex  | Accent L | bg L   | Ratio  | OK? |
|-------------|-------------|----------|--------|--------|-----|
| dark-frost  | `#5AC8FA`   | 0.494    | 0.003  | 10.4   | ✓   |
| dark-frost  | `#FF9F0A`   | 0.395    | 0.003  |  9.6   | ✓   |
| dark-warm   | `#FFB347`   | 0.554    | 0.008  | 10.6   | ✓   |
| light-snow  | `#0A66C2`   | 0.123    | 1.000  |  5.7   | ✓   |
| light-snow  | `#5AC8FA`   | 0.494    | 1.000  |  2.0   | ✗ — too washed out |
| light-cream | `#A8480F`   | 0.103    | 0.828  |  5.1   | ✓   |

The pattern: on dark themes, accents can be quite bright (L ≈ 0.4–0.5)
because the dark bg absorbs the energy. On light themes, accents must
be deeply saturated *and* low-luminance (L ≤ 0.15 for blue, ≤ 0.20 for
warm hues).

---

## 5. Grayscale Ramp Derivation (P2 preview)

The shipped themes carry a *hand-curated* ramp
(`text-primary > text-secondary > text-muted`) anchored to `bg-canvas`.
A future P2 iteration will generate the ramp algorithmically from a
single seed:

1. Pick `text-primary` (must clear 4.5:1, ideally 7:1+).
2. Compute `L_primary`.
3. For each step `i` in `{secondary, muted}`, choose `L_step` by
   linear interpolation in HSL-lightness space, weighted by perceptual
   gamma 2.2 (so steps look evenly spaced to the eye).
4. For dark themes, move *toward* canvas L (L → bg_L); for light
   themes, move *away* from canvas L.
5. Re-compute the hex, snap to sRGB, verify WCAG.

Until then, the hand-curated ramps in the four seed themes serve as
worked examples — copy the pattern, do not copy the literal hex
values, when you build a new theme.

---

## 6. Font Size ↔ Theme Pairing

WCAG AA passes on paper when font size + ratio meet the criteria
together. In projector reality, dark themes project darker than the
file previews; light themes lose contrast under ambient light. **Always
go one notch larger on dark themes.**

### 6.1 Hard floors (all themes)

| Role     | Min size (pt) | Min size (px @ 1280×720) | WCAG floor |
|----------|---------------|--------------------------|------------|
| H1       | **40**        | 40                       | 4.5:1 (3:1 large-text rule, but we anchor to body) |
| H2       | **32**        | 32                       | 4.5:1      |
| Body     | **24**        | 24                       | 4.5:1      |
| Caption  | **18**        | 18 (decorative only)     | n/a        |

These are *minimums*. Treat them as "never go below" — never as
"target".

### 6.2 Recommended sizes by theme

| Role     | Dark themes (recommend) | Light themes (recommend) |
|----------|--------------------------|---------------------------|
| H1       | **44 pt**                | 40 pt                     |
| H2       | **34 pt**                | 32 pt                     |
| Body     | **26 pt**                | 24 pt                     |
| Caption  | 18 pt                    | 16 pt                     |

The 2-pt buffer on dark themes compensates for perceived thinning when
light text is projected on a dimmed screen. Light themes can stay at
the floor because black-on-white already reads sharply.

### 6.3 Anti-patterns

| Bad                                          | Why                                       | Fix                                |
|----------------------------------------------|-------------------------------------------|------------------------------------|
| Body 18pt                                    | Below 24pt floor; fails readability test | 24pt minimum, 26pt on dark themes  |
| H1 32pt with body 28pt (ratio 1.14)          | Heading indistinguishable from body        | H1 ≥ 1.6 × body; on dark, 1.7 × body |
| Caption 14pt on a chart axis                 | Below floor; readable only in print       | 18pt for chart annotations; 16pt if absolutely necessary and bold |
| Different sizes for the same semantic role across pages | Reads as inconsistent typography | Lock sizes in spec_lock; never override per-page |

---

## 7. Workflow — Adopting a Theme

```
  spec_lock.md                  theme JSONs
       │                              │
       │  ## Theme                   │
       │   theme_id: dark-frost  ───▶│ load
       │   min_text_ratio: 4.5       │
       │                              ▼
       │                       executor reads tokens,
       │                       writes var(--token) refs in SVG
       │                              │
       │                              ▼
       │                       finalize_svg expands
       │                       var(--token) → hex
       │                              │
       │                              ▼
       │                       svg_quality_checker
       │                       + contrast_checker verify
       │                              │
       │                              ▼
       │                       svg_to_pptx with audit JSON
       ▼
   contrast_audit.json
   (per text/bg pair with ratio & coords)
```

Operational checklist when adopting a theme:

1. **Strategist writes `## Theme`** block in `spec_lock.md` (see
   `references/strategist.md` — Theme Selection prompt).
2. **Executor references tokens by name** in SVG; no raw hex.
3. **`finalize_svg` expands tokens to hex** if a Theme block is present.
4. **`svg_quality_checker` / `contrast_checker`** run after finalize; any
   ratio below floor is a hard error.
5. **`svg_to_pptx`** writes `contrast_audit.json` per project.
6. **If you change a theme colour**, bump `version` (minor) and
   re-run `validate_theme.py` to refresh `verified_at`.

---

## 8. Adding a New Theme

1. Copy the closest existing theme as a starting template.
2. Pick `bg-canvas` first — it anchors every other ratio.
3. Set `text-primary` to clear 7:1+ (AAA) — easier to relax later than
   to tighten.
4. Set `accent` between 4.5:1 and 12:1 against `bg-canvas`.
5. Hand-derive `text-secondary` and `text-muted` — see §5 for the
   pattern.
6. Bump `version` to `1.0.0` if shipping; keep `0.x.y` if iterating.
7. Run `python3 skills/ppt-master/scripts/validate_theme.py
   skills/ppt-master/themes/<your-theme>.json` — must exit 0.
8. Add an entry in this guide (rationale + palette + use-when).

If you are tempted to ship a theme that fails `validate_theme.py` —
**don't**. The validator exists so the pipeline downstream can trust
the JSON. A theme that bypasses it cascades broken ratios through
every project that adopts it.