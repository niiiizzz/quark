# 03 · UI/UX and Color Palette: Lazarus Dashboard

**Supersedes PRD §10 visual tokens** (the dark-navy theme). Reason: dark navy + blue/indigo accents + Inter + rounded cards is the most common "generated dashboard" look. This system is deliberately different.

---

## 1. Concept: the flight recorder

Lazarus is a system that survives crashes and keeps the evidence. The interface should feel like a **printed operations logbook**: warm paper, black ink, ruled lines, and one burnt-orange accent, the color of something that burned and kept going ("ember"). It reads as a serious instrument, not a product landing page.

### What to avoid (the "AI-generated" tells) and what we do instead

| Tell | Instead |
|---|---|
| Purple/indigo/blue gradients, neon on dark navy | Flat warm paper, black ink, one ember accent |
| Glassmorphism, glows, soft drop shadows everywhere | No shadows. Hairline rules and a 2px ink top rule on cards |
| Large rounded-2xl cards, pill buttons | 3px radius, rectangular buttons |
| Inter + Tailwind defaults | IBM Plex Sans/Mono, Fraunces for page titles, hand-written CSS |
| Emoji or generic icon packs | Small custom inline SVG icons (1.5px stroke, square caps) |
| Gradient text, hero banners, centered marketing layout | Left-aligned, asymmetric, dense, data first |
| "Dashboard", "Welcome back!" headings | Specific titles: "Queue at 14:03:11", "Invariants" |
| Color as the only status signal | Color + shape + word (e.g., `● healthy`, `▲ frozen`, `✕ dead`) |
| Perfectly uniform grid of identical cards | Varied card sizes; the invariants panel and the console feed are visually distinct |

---

## 2. Color tokens (all contrast ratios computed, WCAG 2.x)

### 2.1 Surfaces and ink

| Token | Hex | Role | Check |
|---|---|---|---|
| `--paper` | `#F2EEE3` | Page background | ink on paper **14.86** |
| `--surface` | `#FAF8F1` | Cards, tables, inputs | ink on surface **16.21** |
| `--rule` | `#D9D2BF` | Decorative hairlines (dividers, table rows) | decorative only (1.42), never for control borders |
| `--rule-strong` | `#8F8873` | Borders of inputs, buttons, toggles | **3.33** on surface, **3.05** on paper (meets 3:1 for UI components) |
| `--ink` | `#1C1B18` | Primary text, 2px card top rule | |
| `--ink-2` | `#5A564B` | Secondary text | **6.89** on surface, **6.32** on paper |
| `--ink-3` | `#6F6A5B` | Tertiary text, placeholders, units | **5.08** on surface, **4.66** on paper |

(An earlier candidate `#837D6C` measured only 3.86 on surface and was rejected.)

### 2.2 Accent and status

| Token | Hex | Background pair | Use | Check |
|---|---|---|---|---|
| `--ember` | `#C8401A` | `--on-ember #FFF8F0` | Primary action, active nav bar, focus ring, forecast line | text on ember **4.74**; ember on surface **4.70** |
| `--ember-dark` | `#B03512` | `--on-ember` | Hover/pressed primary; ember text on light | **5.87** on surface; on-ember on it **5.93** |
| `--ember-tint` | `#F4D9CD` | `--ink` | Row hover, selected row | ink on tint **12.84** |
| `--ok` | `#2F5E36` | `--ok-bg #DEE9D6` | Healthy, succeeded, invariant OK | **6.03** on ok-bg; **7.12** on surface; solid fill with on-ember text **7.18** |
| `--warn` | `#7C5100` | `--warn-bg #F2E2B8` | Frozen, retrying, cooling breaker | **5.39** on warn-bg; **6.51** on surface |
| `--bad` | `#9C2418` | `--bad-bg #F1D3CC` | Dead, failed, violated invariant, Kill | **5.57** on bad-bg; **7.38** on surface; solid with on-ember text **7.44** |
| `--info` | `#1F4F63` | `--info-bg #D5E3E9` | Freeze action, neutral info, throughput line | **6.79** on info-bg; **8.38** on surface |

### 2.3 Console (event feed) and sidebar

| Token | Hex | Use | Check |
|---|---|---|---|
| `--console` | `#1E1D1A` | Event feed and log drawers background | |
| `--console-text` | `#E6E0CF` | Log text | **12.78** |
| `--console-ember` | `#F08A63` | Failed/lease-expired lines | **6.84** |
| `--console-ok` | `#8FBF8A` | Success lines | **8.02** |
| `--sidebar` | `#1C1B18` | Left navigation | |
| `--sidebar-text` | `#E6E0CF` | Nav labels | **13.06** |
| `--sidebar-muted` | `#A8A290` | Section captions | **6.75** |
| `--chart-amber` | `#B7791F` | Retry series (graphics only, never text) | **3.43** (graphics need 3:1) |

### 2.4 CSS variables (paste into `app.css`)

```css
:root {
  --paper:#F2EEE3; --surface:#FAF8F1; --rule:#D9D2BF; --rule-strong:#8F8873;
  --ink:#1C1B18; --ink-2:#5A564B; --ink-3:#6F6A5B;
  --ember:#C8401A; --ember-dark:#B03512; --ember-tint:#F4D9CD; --on-ember:#FFF8F0;
  --ok:#2F5E36;   --ok-bg:#DEE9D6;
  --warn:#7C5100; --warn-bg:#F2E2B8;
  --bad:#9C2418;  --bad-bg:#F1D3CC;
  --info:#1F4F63; --info-bg:#D5E3E9;
  --console:#1E1D1A; --console-text:#E6E0CF; --console-ember:#F08A63; --console-ok:#8FBF8A;
  --sidebar:#1C1B18; --sidebar-text:#E6E0CF; --sidebar-muted:#A8A290;
  --chart-amber:#B7791F;

  --font-ui:'IBM Plex Sans', system-ui, sans-serif;
  --font-data:'IBM Plex Mono', ui-monospace, monospace;
  --font-title:'Fraunces', Georgia, serif;
  --radius:3px;
}
body { background:var(--paper); color:var(--ink); font:14px/1.45 var(--font-ui); }
.num { font-family:var(--font-data); font-variant-numeric:tabular-nums; }
```

Proportion rule: about 80% paper/surface, 15% ink, 5% ember and status colors. Ember appears once per screen region (primary action, active nav, key chart line). If everything is orange, nothing is.

---

## 3. Typography

| Role | Font | Size / weight | Notes |
|---|---|---|---|
| Page title | Fraunces | 28px / 600 | One per page, sentence case: "Chaos Lab" |
| Card label | IBM Plex Sans | 11px / 600, uppercase, letter-spacing 0.08em, `--ink-2` | Ledger-style small caps |
| Body, tables | IBM Plex Sans | 14px / 400 | |
| KPI number | IBM Plex Mono | 40px / 500, tabular | |
| Data cells (ids, durations, counters) | IBM Plex Mono | 13px / 400, tabular | Job ids shortened to 8 chars |
| Console | IBM Plex Mono | 12.5px / 400, line-height 1.5 | |
| Buttons | IBM Plex Sans | 13px / 600 | Sentence case |

Fallbacks are system fonts; fonts are self-hosted (`static/fonts`).

## 4. Layout and spacing

- Base unit 4px; spacing scale 4 · 8 · 12 · 16 · 24 · 32.
- Sidebar 208px fixed (`--sidebar`), content area max-width 1280px on `--paper`.
- Cards: `--surface`, 1px `--rule` border, **2px `--ink` top border**, radius 3px, padding 16px, gap 16px. No shadows.
- Desktop-first (1280px laptop); no mobile layout required.
- Left-aligned content. Page title row = title left, page-level actions right.

## 5. Components

### Buttons

| Variant | Look | Used for |
|---|---|---|
| Primary | Ember fill, on-ember text, hover ember-dark | Run chaos test, Apply recommendation, Enqueue |
| Secondary | Surface fill, 1px `--rule-strong` border, ink text | Replay, filters, Refresh |
| Danger | `--bad` fill, on-ember text | Kill worker, Reset demo (confirm step) |
| Cold | `--info-bg` fill, `--info` text, 1px `--info` border | Freeze worker |
| Quiet | Text only, underline on hover | Row links, "View logs" |

States: hover = darker fill; active = 1px translateY; focus = 2px ember outline with 2px offset; disabled = 45% opacity, `cursor:not-allowed`, tooltip says why; loading = label replaced by "Working…" and button disabled (no spinner animation needed). Height 32px (row actions 28px), padding 0 12px, radius 3px.

### Toggle (Fencing, Outage, Autoscale)
Rectangular track with a square thumb. Label on the left, state word on the right ("ON"/"OFF"). Fencing OFF turns track and word `--bad` and a one-line caption appears: "Unsafe. This is the bug fencing prevents."

### Status badges
`● healthy` (ok), `▲ frozen` (warn), `✕ dead` (bad), `◐ restarting` (ink-2), `● closed / ✕ open` for breakers. Badge = tinted background + colored text + shape, radius 2px, 12px text.

### Tables
Hairline `--rule` row separators, no zebra, header row in small caps with 2px ink bottom rule, row hover `--ember-tint`, data cells in mono. Sticky header in scrollable tables.

### Invariants panel
A distinct card: heading "Invariants", five rows, each `✓` (ok) or `✕` (bad) + name + live value in mono. On any violation the card gets a 3px `--bad` border and a banner "INVARIANT VIOLATED" (bad solid, on-ember text, 7.44).

### Event console
`--console` background, mono 12.5px, newest at top, 15 lines, columns: time · event · job · worker. Failed or expired lines in `--console-ember`, succeeded in `--console-ok`, everything else `--console-text`.

### Toast
Bottom-left, ink background, paper text, log style: `14:03:11  killed worker-2-a3f1`. Auto-dismiss 4s, `aria-live="polite"`.

### Drawer (job detail)
Slides in from the right (160ms), 480px wide, same card styling, event timeline as a vertical ruled list.

## 6. Charts (Chart.js, flat)

- No fills or gradients, line width 2px, point radius 0, gridlines `--rule`, ticks `--ink-2` 11px Plex Mono, no chart borders or legends in boxes (inline labels).
- **Color is never the only encoding:** different dash patterns too.

| Chart | Series | Style |
|---|---|---|
| Throughput (60s) | Succeeded/s | `--ok`, solid |
| | Failed/s | `--ember`, dashed 6/4 |
| | Retried/s (optional) | `--chart-amber`, dotted |
| flaky_webhook attempts/s (Insights) | Attempts | `--info`, solid; breaker-open window shaded with `--bad-bg` |
| Forecast | Actual arrival rate | `--ink`, solid |
| | Forecast | `--ember`, dashed |
| | Workers (current vs recommended) | step lines, `--ink-2` and `--ember` |

## 7. Iconography

Inline SVG, 16px, 1.5px stroke, square caps and miter joins, `currentColor`. Needed set: overview, workers, jobs, dlq, chaos, insights, kill, freeze, replay, check, cross, warning, close, chevron. No emoji anywhere. Brand: keep the Quark logo as supplied from the deck, on a `--surface` chip in the sidebar header; wordmark "Lazarus" in Fraunces beside it.

## 8. Voice and microcopy

Plain, specific, sentence case, no exclamation marks, numbers with units.

| Instead of | Write |
|---|---|
| "Oops! Something went wrong" | "Lost contact with the API. Retrying every 2s." |
| "Terminate instance" | "Kill worker" |
| "Awesome, test complete!" | "Chaos run complete: PASS" |
| "No data" | "No jobs yet. Run a demo load." |
| "Fencing disabled" | "Fencing OFF. Unsafe: duplicates are possible." |

## 9. Motion

Functional only: card border flash 600ms when a value changes (`--ember` to `--rule`), drawer slide 160ms, number changes with no counting animation. `@media (prefers-reduced-motion: reduce)` disables all of it.

## 10. Accessibility

- Every text/background pair above is ≥ 4.5:1; control borders ≥ 3:1; graphics ≥ 3:1.
- Status = color + shape + text. Chart series = color + dash.
- Keyboard: all buttons and tabs reachable; visible 2px ember focus ring; Esc closes the drawer.
- Toasts and invariant banner use `aria-live`. Tables use real `<th scope>`.
- The paper theme prints cleanly, which helps if you attach report-card screenshots or PDFs.

## 11. Design acceptance checklist

- [ ] No gradient, shadow or glow anywhere (search the CSS for `gradient`, `box-shadow`, `blur`).
- [ ] No hex values in components, only variables.
- [ ] Ember used on ≤ 3 elements per screen.
- [ ] Every status shows a shape and a word.
- [ ] KPI numbers and all ids and durations are Plex Mono, tabular.
- [ ] Fonts and scripts load with the network disabled.
