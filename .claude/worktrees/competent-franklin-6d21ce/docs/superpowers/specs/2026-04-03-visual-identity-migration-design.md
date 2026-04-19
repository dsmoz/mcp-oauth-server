# Visual Identity Migration — Design Spec

**Date:** 2026-04-03  
**Status:** Approved  
**Roadmap item:** UX Polish — Apply DSMOZ Intelligence visual identity to all admin and public-facing templates

---

## Context

v1.3 delivered `docs/visual-identity-guide.html` — the DSMOZ Intelligence brand system. All 13 Jinja2 templates still use the old violet/neutral-dark scheme (`#6d28d9` buttons, `#0f1117` backgrounds, system font stack). This migration replaces that scheme with the teal/orange brand, adds a sidebar layout with mobile icon rail, and adds Phosphor Light icons throughout.

---

## Decisions

| Topic | Decision |
|---|---|
| Admin layout | Sidebar (220px desktop) |
| Mobile nav | Icon rail (40px, CSS-only, no JS) |
| Public pages (consent, register) | Light white card on `#060E10` dark background |
| Icons | Phosphor Light — nav items, buttons, table row actions |
| CSS architecture | CSS custom properties in `base.html` — no new static files |

---

## CSS Token System

All tokens defined once in `base.html` `:root {}`. Child templates reference variables only — no hardcoded hex values.

```css
:root {
  /* Backgrounds */
  --bg:          #060E10;
  --surface:     #0A1C20;
  --elevated:    #0D2A30;
  --border:      #16464F;
  --focus:       #115E67;

  /* Accent (orange) */
  --accent:      #E8500A;   /* buttons */
  --accent-vivid:#FF5E00;   /* brand wordmark, hover */
  --accent-dim:  #C4420A;   /* pressed/active */
  --amber:       #FFAE62;   /* credential text, warnings */

  /* Typography */
  --text-h:      #F0F7F8;   /* headings */
  --text-b:      #D4E8EA;   /* body */
  --text-ui:     #91BCC1;   /* labels, nav */
  --text-muted:  #5A8A90;   /* hints */
  --text-dim:    #2E5A60;   /* placeholders, meta */

  /* Semantic */
  --success-bg:  #073D20;
  --success-fg:  #3DD68C;
  --danger-bg:   #3D0A0A;
  --danger-fg:   #FF6B6B;
  --warn-bg:     #3D2200;
  --warn-fg:     #FFAE62;
  --info-bg:     #052830;
  --info-fg:     #7BCFD8;

  /* Credential / secret */
  --secret-bg:   #1A0C00;
  --secret-border:#7A3000;
}
```

---

## Layout: base.html

### Structure change
Replace the current `<nav>` top bar with a full-page flex layout:

```
<body>
  <div class="app-shell">          ← flex row, 100vw × 100vh
    <aside class="sidebar">        ← 220px desktop / 40px mobile
    <main class="main-content">    ← flex:1, overflow-y:auto
```

### Sidebar (desktop ≥768px)
- Width: 220px
- Background: `--surface`
- Border-right: `1px solid --border`
- Brand wordmark top: "DS-MOZ INTELLIGENCE" in `--accent-vivid`, weight 800
- Section labels ("Management", "System") in `--text-dim`, 0.6rem uppercase
- Nav items: icon (18px Phosphor Light) + label, `--text-ui` default, `--text-h` active
- Active state: `background: --elevated`, `border-left: 2px solid --accent-vivid`
- Nav links: Dashboard, Clients, Registrations, Discovery

### Icon rail (mobile <768px)
- CSS `@media (max-width: 767px)` collapses sidebar to 40px
- Hide all `.nav-label` text
- Hide `.sidebar-section-label`
- Brand collapses to "DS" in `writing-mode: vertical-rl`
- Icons retain `title` attribute for native tooltip
- Active icon: `--accent-vivid`
- No JavaScript required

### Main content area
- `padding: 1.5rem 2rem` desktop, `padding: 1rem` mobile
- `max-width: none` (full width minus sidebar)

---

## Component Updates

### Buttons
| Class | Before | After |
|---|---|---|
| `.btn-primary` | `#6d28d9` | `background: var(--accent); color: #fff` |
| `.btn-secondary` | `#1e2235` | `background: transparent; border: 1px solid var(--border); color: var(--text-ui)` |
| `.btn-danger` | `#7f1d1d` | `background: var(--danger-bg); color: var(--danger-fg); border: 1px solid #5A1010` |

All buttons gain an icon slot: `<i class="ph-light ph-..."></i>` before the label.

### Badges
| Class | Before | After |
|---|---|---|
| `.badge-active` | `#14532d / #4ade80` | `var(--success-bg) / var(--success-fg)` |
| `.badge-inactive` | `#450a0a / #f87171` | `var(--border) / var(--text-ui)` |
| `.badge-pending` | `#78350f / #fbbf24` | `var(--warn-bg) / var(--warn-fg)` |

### Form inputs
- Background: `var(--elevated)`
- Border: `1px solid var(--border)`
- Focus border: `var(--focus)`
- Font: `'Avenir Next', 'Avenir', 'Segoe UI', Helvetica Neue, Arial, sans-serif`

### Cards
- Background: `var(--surface)`
- Border: `1px solid var(--border)`
- Border-radius: 8px

### Table
- Header background: `var(--elevated)`
- Header text: `var(--text-muted)`, 0.65rem uppercase
- Row border: `1px solid var(--border)`
- Row hover: `background: var(--elevated)`
- Row actions column: icon-only (`ph-light ph-eye`, `ph-pencil-simple`, `ph-key`, `ph-trash`) in `var(--text-muted)`, 16px

### Secret box
- Background: `var(--secret-bg)` (`#1A0C00`)
- Border: `1px solid var(--secret-border)` (`#7A3000`)
- Label: `var(--accent-vivid)`, uppercase, key icon prefix
- Value: `var(--amber)`, monospace
- Warning: `var(--amber)` at 70% opacity

### Info zone (rotate secret section)
- Background: `var(--surface)`
- Border: `1px solid var(--focus)`
- Heading: `var(--info-fg)` (`#7BCFD8`)

### Danger zone
- Background: `var(--danger-bg)`
- Border: `1px solid #5A1010`
- Heading: `var(--danger-fg)`

### Stats grid (dashboard)
- 4 columns desktop, 2 columns mobile
- Alert card (pending): `background: var(--secret-bg); border-color: var(--secret-border)`
- Alert number: `var(--amber)`

---

## Icon Map

| Location | Icon |
|---|---|
| Dashboard nav | `ph-squares-four` |
| Clients nav | `ph-identification-card` |
| Registrations nav | `ph-clipboard-text` |
| Discovery nav | `ph-globe` |
| New client button | `ph-plus` |
| Edit button | `ph-pencil-simple` |
| Rotate secret | `ph-arrows-clockwise` |
| View tokens | `ph-coins` |
| Revoke/danger | `ph-prohibit` |
| Delete | `ph-trash` |
| View/detail | `ph-eye` |
| Key/secret | `ph-key` |
| Approve | `ph-check` |
| Reject | `ph-x` |
| Submit / send | `ph-paper-plane-tilt` |
| Back arrow | `ph-arrow-left` |

Import: `<link rel="stylesheet" href="https://unpkg.com/@phosphor-icons/web@2.1.1/src/light/style.css">`  
Added to `base.html` `<head>` only. Standalone templates (consent, register) include it directly.

---

## Public Pages (Standalone — no base.html)

Applies to: `consent.html`, `consent_waiting.html`, `register.html`, `register_success.html`

### Layout
```
body { background: var(--bg); display: flex; align-items: center; justify-content: center; min-height: 100vh }
.card { background: #fff; border-radius: 12px; padding: 1.75rem; max-width: 400px; width: 100%; box-shadow: 0 12px 48px rgba(0,0,0,0.6) }
```

### Card contents
- Brand line: "DS-MOZ Intelligence" in `--accent-vivid`, 0.65rem uppercase
- Title: `#0A1C20`, weight 700
- Body text: `#5A8A90`
- Form fields: `background: #F0F7F8; border: 1px solid #D4E8EA` (light teal)
- Focus: `border-color: #115E67`
- Submit button: `background: var(--accent)`, full width, with icon

### consent_waiting.html — preserve JS polling
The polling logic (`poll()`, `scheduleNext()`, `showError()`) stays intact. Only visual changes:
- Spinner border: `#D4E8EA` (rest), `#115E67` (top) — teal instead of purple
- Error message shown inside white card

---

## Files to Modify

| File | Change type |
|---|---|
| `src/admin/templates/base.html` | Full rewrite — CSS tokens, sidebar layout, Phosphor import |
| `src/admin/templates/dashboard.html` | Colour token swap, icon on "New Client" button, stat colours |
| `src/admin/templates/clients_list.html` | Icon on "+ New Client", table row action icons |
| `src/admin/templates/client_detail.html` | Secret box, info/danger zone colours, button icons |
| `src/admin/templates/client_create.html` | Info card → `--info` tokens, button icon |
| `src/admin/templates/client_edit.html` | Button icon |
| `src/admin/templates/client_tokens.html` | Revoke button icon |
| `src/admin/templates/registrations_list.html` | Table row icon buttons |
| `src/admin/templates/registration_detail.html` | Info card tokens, approve/reject button icons |
| `src/admin/templates/consent.html` | Full standalone CSS rewrite → light card |
| `src/admin/templates/consent_waiting.html` | Full standalone CSS rewrite → light card; preserve JS |
| `src/admin/templates/register.html` | Full standalone CSS rewrite → light card |
| `src/admin/templates/register_success.html` | Full standalone CSS rewrite → light card |

---

## Verification

1. **Visual check — admin panel:** Open `/admin/` in browser. Confirm sidebar visible, teal/orange palette, orange active nav indicator, Phosphor icons in nav and table actions.
2. **Visual check — mobile:** Resize to <768px. Confirm sidebar collapses to 40px icon rail, labels hidden, icons remain.
3. **Visual check — public pages:** Open `/register` and `/authorize/consent?session=test`. Confirm white card on dark background, light form fields, orange submit button.
4. **Polling preserved:** Open a real or mock consent_waiting page. Confirm spinner visible and JS polling fires (check Network tab for `/consent/status` requests).
5. **Secret display:** Approve a registration request to trigger the secret-shown-once flow. Confirm amber text on dark brown background.
6. **Dark/danger zones:** Open a client detail page. Confirm teal info border on rotate section, red danger zone.
7. **No regressions:** All form submissions, admin actions (create/edit/rotate/revoke/delete/approve/reject) work as before — only CSS changed, no backend or route changes.
