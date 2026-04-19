# Visual Identity Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the violet/neutral-dark colour scheme across all 13 Jinja2 templates with the DSMOZ Intelligence teal/orange brand, add a responsive sidebar layout (220px desktop → 40px icon rail mobile), and add Phosphor Light icons throughout.

**Architecture:** All CSS tokens are centralised in `base.html`'s `:root {}` block as CSS custom properties. Child templates reference only `var(--token-name)` — no hardcoded hex values. The sidebar layout replaces the current top nav using a CSS-only media query to collapse to an icon rail at `<768px`. Standalone public pages (consent, register) are rewritten independently using the same token values inlined.

**Tech Stack:** Jinja2 templates, pure CSS (custom properties + media queries), Phosphor Icons CDN (`@phosphor-icons/web@2.1.1`), no build step, no JavaScript changes.

---

## File Map

| File | Change |
|---|---|
| `src/admin/templates/base.html` | Full rewrite — CSS tokens, sidebar, Phosphor import |
| `src/admin/templates/dashboard.html` | Token swap for inline colour overrides |
| `src/admin/templates/clients_list.html` | Button icons, empty state link colour |
| `src/admin/templates/client_detail.html` | Secret box, info/danger zone tokens, button icons |
| `src/admin/templates/client_create.html` | Info card tokens, button icon |
| `src/admin/templates/client_edit.html` | Button icons |
| `src/admin/templates/client_tokens.html` | Button icon, expired badge token |
| `src/admin/templates/registrations_list.html` | Button icon |
| `src/admin/templates/registration_detail.html` | Info/actions card tokens, button icons |
| `src/admin/templates/consent.html` | Full rewrite — light card layout |
| `src/admin/templates/consent_waiting.html` | Full rewrite — light card layout, preserve JS |
| `src/admin/templates/register.html` | Full rewrite — light card layout |
| `src/admin/templates/register_success.html` | Full rewrite — light card layout |

---

## Task 1: Rewrite base.html

This is the foundation. All other admin pages inherit from it.

**Files:**
- Modify: `src/admin/templates/base.html`

- [ ] **Step 1: Replace base.html with the new sidebar layout and CSS token system**

Replace the entire file with:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}DS-MOZ MCP OAuth{% endblock %}</title>
  <link rel="stylesheet" href="https://unpkg.com/@phosphor-icons/web@2.1.1/src/light/style.css">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg:           #060E10;
      --surface:      #0A1C20;
      --elevated:     #0D2A30;
      --border:       #16464F;
      --focus:        #115E67;
      --accent:       #E8500A;
      --accent-vivid: #FF5E00;
      --accent-dim:   #C4420A;
      --amber:        #FFAE62;
      --text-h:       #F0F7F8;
      --text-b:       #D4E8EA;
      --text-ui:      #91BCC1;
      --text-muted:   #5A8A90;
      --text-dim:     #2E5A60;
      --success-bg:   #073D20;
      --success-fg:   #3DD68C;
      --danger-bg:    #3D0A0A;
      --danger-fg:    #FF6B6B;
      --warn-bg:      #3D2200;
      --warn-fg:      #FFAE62;
      --info-bg:      #052830;
      --info-fg:      #7BCFD8;
      --secret-bg:    #1A0C00;
      --secret-border:#7A3000;
    }

    body {
      font-family: 'Avenir Next', 'Avenir', 'Segoe UI', Helvetica Neue, Arial, sans-serif;
      background: var(--bg);
      color: var(--text-b);
      min-height: 100vh;
    }

    /* ---- App shell ---- */
    .app-shell {
      display: flex;
      min-height: 100vh;
    }

    /* ---- Sidebar ---- */
    .sidebar {
      width: 220px;
      flex-shrink: 0;
      background: var(--surface);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      position: sticky;
      top: 0;
      height: 100vh;
      overflow-y: auto;
    }

    .sidebar-brand {
      padding: 1.25rem 1.25rem 1rem;
      font-size: 0.7rem;
      font-weight: 800;
      letter-spacing: 0.1em;
      color: var(--accent-vivid);
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
      overflow: hidden;
    }

    .sidebar-section { padding: 0.5rem 0; }

    .sidebar-section-label {
      padding: 0.5rem 1.25rem 0.25rem;
      font-size: 0.6rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: var(--text-dim);
    }

    .nav-item {
      display: flex;
      align-items: center;
      gap: 0.625rem;
      padding: 0.55rem 1.25rem;
      color: var(--text-ui);
      text-decoration: none;
      font-size: 0.8125rem;
      transition: color 0.1s, background 0.1s;
      white-space: nowrap;
      overflow: hidden;
    }

    .nav-item i { font-size: 18px; flex-shrink: 0; }

    .nav-item:hover {
      color: var(--text-h);
      background: var(--elevated);
    }

    .nav-item.active {
      color: var(--text-h);
      background: var(--elevated);
      border-left: 2px solid var(--accent-vivid);
      padding-left: calc(1.25rem - 2px);
    }

    /* ---- Main content ---- */
    .main-content {
      flex: 1;
      padding: 1.75rem 2rem;
      overflow-x: hidden;
    }

    /* ---- Typography ---- */
    h1 {
      font-size: 1.375rem;
      font-weight: 700;
      color: var(--text-h);
      margin-bottom: 1.5rem;
    }

    h2 {
      font-size: 1.125rem;
      font-weight: 600;
      color: var(--text-h);
      margin-bottom: 1rem;
    }

    /* ---- Table ---- */
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.875rem;
    }

    th {
      text-align: left;
      padding: 0.65rem 1rem;
      background: var(--elevated);
      color: var(--text-muted);
      font-weight: 700;
      font-size: 0.65rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      border-bottom: 1px solid var(--border);
    }

    td {
      padding: 0.875rem 1rem;
      border-bottom: 1px solid var(--border);
      color: var(--text-b);
      vertical-align: middle;
    }

    tr:hover td { background: var(--elevated); }

    /* ---- Badges ---- */
    .badge {
      display: inline-block;
      padding: 0.2em 0.6em;
      border-radius: 9999px;
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .badge-active   { background: var(--success-bg); color: var(--success-fg); }
    .badge-inactive { background: var(--border);     color: var(--text-ui); }
    .badge-pending  { background: var(--warn-bg);    color: var(--warn-fg); }

    /* ---- Buttons ---- */
    .btn {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      padding: 0.5rem 1rem;
      border-radius: 6px;
      font-size: 0.8125rem;
      font-weight: 600;
      cursor: pointer;
      border: none;
      text-decoration: none;
      transition: opacity 0.15s;
      white-space: nowrap;
    }

    .btn:hover { opacity: 0.85; }
    .btn i { font-size: 15px; }

    .btn-primary   { background: var(--accent);    color: #fff; }
    .btn-secondary { background: transparent; color: var(--text-ui); border: 1px solid var(--border); }
    .btn-danger    { background: var(--danger-bg); color: var(--danger-fg); border: 1px solid #5A1010; }

    /* ---- Forms ---- */
    .form-group { margin-bottom: 1.25rem; }

    label {
      display: block;
      margin-bottom: 0.4rem;
      font-size: 0.8125rem;
      font-weight: 600;
      color: var(--text-ui);
    }

    input[type="text"],
    input[type="password"],
    input[type="email"],
    textarea {
      width: 100%;
      padding: 0.6rem 0.875rem;
      background: var(--elevated);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text-h);
      font-size: 0.875rem;
      font-family: inherit;
      outline: none;
      transition: border-color 0.15s;
    }

    input:focus, textarea:focus { border-color: var(--focus); }

    textarea { resize: vertical; min-height: 80px; }

    small {
      display: block;
      margin-top: 0.25rem;
      font-size: 0.75rem;
      color: var(--text-muted);
    }

    /* ---- Cards ---- */
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1.5rem;
      margin-bottom: 1.25rem;
    }

    /* ---- Secret box ---- */
    .secret-box {
      background: var(--secret-bg);
      border: 1px solid var(--secret-border);
      border-radius: 8px;
      padding: 1.25rem 1.5rem;
      margin-bottom: 1.5rem;
    }

    .secret-box .label {
      font-size: 0.7rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent-vivid);
      margin-bottom: 0.5rem;
      display: flex;
      align-items: center;
      gap: 0.35rem;
    }

    .secret-box .warning {
      font-size: 0.8rem;
      color: var(--amber);
      opacity: 0.75;
      margin-top: 0.5rem;
    }

    .secret-value {
      font-family: 'SFMono-Regular', Consolas, monospace;
      font-size: 0.9rem;
      color: var(--amber);
      word-break: break-all;
      padding: 0.5rem 0;
    }

    /* ---- Error box ---- */
    .error-box {
      background: var(--danger-bg);
      border: 1px solid #5A1010;
      border-radius: 6px;
      padding: 0.75rem 1rem;
      color: var(--danger-fg);
      font-size: 0.875rem;
      margin-bottom: 1rem;
    }

    /* ---- Detail rows ---- */
    .detail-row {
      display: flex;
      gap: 1rem;
      padding: 0.625rem 0;
      border-bottom: 1px solid var(--border);
      font-size: 0.875rem;
    }

    .detail-row:last-child { border-bottom: none; }

    .detail-label {
      flex: 0 0 160px;
      color: var(--text-muted);
      font-weight: 600;
    }

    .detail-value {
      color: var(--text-b);
      font-family: 'SFMono-Regular', Consolas, monospace;
      font-size: 0.8125rem;
      word-break: break-all;
    }

    /* ---- Layout helpers ---- */
    .actions { display: flex; gap: 0.5rem; align-items: center; }

    .page-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 1.5rem;
    }

    .page-header h1 { margin-bottom: 0; }

    /* ---- Stats grid ---- */
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 1rem;
      margin-bottom: 1.5rem;
    }

    .stat-card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 1.25rem 1.5rem;
    }

    .stat-card-alert {
      background: var(--secret-bg);
      border-color: var(--secret-border);
    }

    .stat-number {
      font-size: 2rem;
      font-weight: 800;
      color: var(--text-h);
      line-height: 1;
      margin-bottom: 0.4rem;
    }

    .stat-label {
      font-size: 0.8125rem;
      color: var(--text-muted);
      font-weight: 600;
    }

    /* ---- Mobile: icon rail ---- */
    @media (max-width: 767px) {
      .sidebar {
        width: 40px;
        overflow: visible;
      }

      .sidebar-brand {
        padding: 0.75rem 0;
        text-align: center;
        font-size: 0.55rem;
        letter-spacing: 0.05em;
        writing-mode: vertical-rl;
        border-bottom: none;
        border-bottom: 1px solid var(--border);
        height: 52px;
        display: flex;
        align-items: center;
        justify-content: center;
      }

      .sidebar-section-label { display: none; }

      .nav-item {
        padding: 0.6rem 0;
        justify-content: center;
        border-left: none !important;
        padding-left: 0 !important;
      }

      .nav-item.active {
        border-left: none;
        border-right: 2px solid var(--accent-vivid);
        padding-left: 0 !important;
      }

      .nav-label { display: none; }

      .main-content { padding: 1rem; }

      .stats-grid { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="sidebar-brand">DS-MOZ INTELLIGENCE</div>
      <nav class="sidebar-section">
        <div class="sidebar-section-label">Management</div>
        <a href="/admin/" class="nav-item {% if request.url.path == '/admin/' %}active{% endif %}"
           title="Dashboard">
          <i class="ph-light ph-squares-four"></i>
          <span class="nav-label">Dashboard</span>
        </a>
        <a href="/admin/clients/" class="nav-item {% if '/admin/clients' in request.url.path %}active{% endif %}"
           title="Clients">
          <i class="ph-light ph-identification-card"></i>
          <span class="nav-label">Clients</span>
        </a>
        <a href="/admin/registrations" class="nav-item {% if '/admin/registrations' in request.url.path %}active{% endif %}"
           title="Registrations">
          <i class="ph-light ph-clipboard-text"></i>
          <span class="nav-label">Registrations</span>
        </a>
        <div class="sidebar-section-label" style="margin-top: 0.5rem;">System</div>
        <a href="/.well-known/openid-configuration" class="nav-item" title="Discovery">
          <i class="ph-light ph-globe"></i>
          <span class="nav-label">Discovery</span>
        </a>
      </nav>
    </aside>
    <main class="main-content">
      {% block content %}{% endblock %}
    </main>
  </div>
</body>
</html>
```

- [ ] **Step 2: Verify the server starts and admin panel loads**

```bash
cd /Users/danilodasilva/Documents/Programming/mcp-oauth-server
uv run uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/admin/ — expect: teal sidebar on the left, orange brand wordmark, teal background. On mobile (<768px): 40px icon rail.

- [ ] **Step 3: Commit**

```bash
git add src/admin/templates/base.html
git commit -m "feat: rebrand base.html — teal/orange sidebar, CSS tokens, Phosphor icons"
```

---

## Task 2: Update dashboard.html

Remove hardcoded colour overrides; use CSS tokens.

**Files:**
- Modify: `src/admin/templates/dashboard.html`

- [ ] **Step 1: Replace inline colour overrides with token-based values**

Replace the entire file with:

```html
{% extends "base.html" %}
{% block title %}Dashboard — DS-MOZ MCP OAuth{% endblock %}

{% block content %}
<div class="page-header">
  <h1>Dashboard</h1>
  <a href="/admin/clients/new" class="btn btn-primary">
    <i class="ph-light ph-plus"></i> New Client
  </a>
</div>

<div class="stats-grid">
  <div class="stat-card">
    <div class="stat-number">{{ total_clients }}</div>
    <div class="stat-label">Total Clients</div>
  </div>
  <div class="stat-card">
    <div class="stat-number" style="color: var(--success-fg);">{{ active_clients }}</div>
    <div class="stat-label">Active Clients</div>
  </div>
  <div class="stat-card">
    <div class="stat-number" style="color: var(--info-fg);">{{ active_tokens }}</div>
    <div class="stat-label">Active Tokens</div>
  </div>
  <div class="stat-card {% if pending_requests > 0 %}stat-card-alert{% endif %}">
    <div class="stat-number" style="{% if pending_requests > 0 %}color: var(--amber);{% endif %}">
      {{ pending_requests }}
    </div>
    <div class="stat-label">
      Pending Requests
      {% if pending_requests > 0 %}
        <a href="/admin/registrations" style="color: var(--amber); margin-left: 0.5rem; font-size: 0.7rem;">Review →</a>
      {% endif %}
    </div>
  </div>
</div>

<div class="card">
  <h2>Recent Clients</h2>
  {% if recent_clients %}
  <table>
    <thead>
      <tr>
        <th>Name</th>
        <th>Client ID</th>
        <th>Status</th>
        <th>Created</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody>
      {% for client in recent_clients %}
      <tr>
        <td>{{ client.client_name }}</td>
        <td style="font-family: monospace; font-size: 0.8rem;">{{ client.client_id }}</td>
        <td>
          {% if client.is_active %}
            <span class="badge badge-active">Active</span>
          {% else %}
            <span class="badge badge-inactive">Revoked</span>
          {% endif %}
        </td>
        <td style="color: var(--text-muted); font-size: 0.8rem;">
          {{ client.created_at[:10] if client.created_at else "—" }}
        </td>
        <td>
          <a href="/admin/clients/{{ client.client_id }}" class="btn btn-secondary">
            <i class="ph-light ph-eye"></i> View
          </a>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p style="color: var(--text-muted); font-size: 0.875rem;">No clients yet.</p>
  {% endif %}
</div>
{% endblock %}
```

- [ ] **Step 2: Verify in browser**

Reload http://localhost:8000/admin/ — stats grid should show teal success colour for active clients, info teal for tokens, amber alert for pending requests.

- [ ] **Step 3: Commit**

```bash
git add src/admin/templates/dashboard.html
git commit -m "feat: rebrand dashboard — token colours, icons"
```

---

## Task 3: Update clients_list.html

Add icons to buttons; fix empty-state link colour.

**Files:**
- Modify: `src/admin/templates/clients_list.html`

- [ ] **Step 1: Replace the file**

```html
{% extends "base.html" %}
{% block title %}Clients — DS-MOZ MCP OAuth{% endblock %}

{% block content %}
<div class="page-header">
  <h1>OAuth Clients</h1>
  <div class="actions">
    <a href="/admin/registrations" class="btn btn-secondary">
      <i class="ph-light ph-clipboard-text"></i> Registrations
    </a>
    <a href="/admin/clients/new" class="btn btn-primary">
      <i class="ph-light ph-plus"></i> New Client
    </a>
  </div>
</div>

{% if clients %}
<table>
  <thead>
    <tr>
      <th>Name</th>
      <th>Client ID</th>
      <th>Status</th>
      <th>Created</th>
      <th>Actions</th>
    </tr>
  </thead>
  <tbody>
    {% for client in clients %}
    <tr>
      <td>{{ client.client_name }}</td>
      <td style="font-family: monospace; font-size: 0.8rem;">{{ client.client_id }}</td>
      <td>
        {% if client.is_active %}
          <span class="badge badge-active">Active</span>
        {% else %}
          <span class="badge badge-inactive">Revoked</span>
        {% endif %}
      </td>
      <td style="color: var(--text-muted); font-size: 0.8rem;">
        {{ client.created_at[:10] if client.created_at else "—" }}
      </td>
      <td class="actions">
        <a href="/admin/clients/{{ client.client_id }}" class="btn btn-secondary">
          <i class="ph-light ph-eye"></i> View
        </a>
        {% if client.is_active %}
        <form method="post" action="/admin/clients/{{ client.client_id }}/revoke"
              onsubmit="return confirm('Revoke all tokens for {{ client.client_name }}?')">
          <button type="submit" class="btn btn-danger">
            <i class="ph-light ph-prohibit"></i> Revoke
          </button>
        </form>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<div class="card" style="text-align: center; color: var(--text-muted); padding: 3rem;">
  <p>No clients yet. <a href="/admin/clients/new" style="color: var(--accent-vivid);">Create your first client</a>.</p>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 2: Verify in browser**

Open http://localhost:8000/admin/clients/ — buttons should have icons, empty state link orange.

- [ ] **Step 3: Commit**

```bash
git add src/admin/templates/clients_list.html
git commit -m "feat: rebrand clients list — icons, token colours"
```

---

## Task 4: Update client_detail.html

Migrate secret box, info zone, danger zone to CSS tokens; add button icons.

**Files:**
- Modify: `src/admin/templates/client_detail.html`

- [ ] **Step 1: Replace the file**

```html
{% extends "base.html" %}
{% block title %}{{ client.client_name }} — DS-MOZ MCP OAuth{% endblock %}

{% block content %}
<div class="page-header">
  <h1>{{ client.client_name }}</h1>
  <div class="actions">
    <a href="/admin/clients/{{ client.client_id }}/tokens" class="btn btn-secondary">
      <i class="ph-light ph-coins"></i> Tokens
    </a>
    <a href="/admin/clients/{{ client.client_id }}/edit" class="btn btn-secondary">
      <i class="ph-light ph-pencil-simple"></i> Edit
    </a>
    <a href="/admin/clients/" class="btn btn-secondary">
      <i class="ph-light ph-arrow-left"></i> Clients
    </a>
  </div>
</div>

{% if secret %}
<div class="secret-box">
  <p class="label"><i class="ph-light ph-key"></i> Client Secret — Save This Now</p>
  <div class="secret-value">{{ secret }}</div>
  <p class="warning">This secret will never be shown again. Copy it now and store it securely.</p>
</div>
{% endif %}

<div class="card">
  <h2>Client Details</h2>

  <div class="detail-row">
    <span class="detail-label">Client ID</span>
    <span class="detail-value">{{ client.client_id }}</span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Name</span>
    <span class="detail-value" style="font-family: inherit; color: var(--text-h);">{{ client.client_name }}</span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Status</span>
    <span class="detail-value">
      {% if client.is_active %}
        <span class="badge badge-active">Active</span>
      {% else %}
        <span class="badge badge-inactive">Revoked</span>
      {% endif %}
    </span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Scope</span>
    <span class="detail-value">{{ client.scope or "mcp" }}</span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Grant Types</span>
    <span class="detail-value">{{ (client.grant_types or ["authorization_code"]) | join(", ") }}</span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Redirect URIs</span>
    <span class="detail-value">
      {% if client.redirect_uris %}
        {% for uri in client.redirect_uris %}
          <div>{{ uri }}</div>
        {% endfor %}
      {% else %}
        <span style="color: var(--text-muted); font-family: inherit; font-style: italic;">Any (unrestricted)</span>
      {% endif %}
    </span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Created By</span>
    <span class="detail-value" style="font-family: inherit;">{{ client.created_by or "—" }}</span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Created At</span>
    <span class="detail-value" style="font-family: inherit;">{{ client.created_at or "—" }}</span>
  </div>
</div>

<div class="card" style="border-color: var(--focus);">
  <h2 style="color: var(--info-fg);">Rotate Secret</h2>
  <p style="color: var(--text-muted); font-size: 0.8125rem; margin-bottom: 1rem; line-height: 1.6;">
    Generates a new client secret. Existing access tokens remain valid. The old secret is
    immediately invalidated for new token requests.
  </p>
  <form method="post" action="/admin/clients/{{ client.client_id }}/rekey"
        onsubmit="return confirm('Rotate the secret for {{ client.client_name }}? The old secret will stop working immediately.')">
    <button type="submit" class="btn btn-secondary">
      <i class="ph-light ph-arrows-clockwise"></i> Rotate Secret
    </button>
  </form>
</div>

{% if client.is_active %}
<div class="card" style="border-color: #5A1010; background: var(--danger-bg);">
  <h2 style="color: var(--danger-fg);">Danger Zone</h2>

  <p style="color: var(--text-muted); font-size: 0.8125rem; margin-bottom: 0.75rem; line-height: 1.6;">
    <strong style="color: var(--text-ui);">Revoke</strong> — deactivates the client and invalidates all tokens.
    The client row is kept for audit purposes. Cannot be reactivated.
  </p>
  <form method="post" action="/admin/clients/{{ client.client_id }}/revoke"
        onsubmit="return confirm('Revoke {{ client.client_name }} and all its tokens?')"
        style="margin-bottom: 1.25rem;">
    <button type="submit" class="btn btn-danger">
      <i class="ph-light ph-prohibit"></i> Revoke Client & All Tokens
    </button>
  </form>

  <p style="color: var(--text-muted); font-size: 0.8125rem; margin-bottom: 0.75rem; line-height: 1.6;">
    <strong style="color: var(--text-ui);">Delete</strong> — permanently removes this client and all associated
    tokens from the database. This is irreversible.
  </p>
  <form method="post" action="/admin/clients/{{ client.client_id }}/delete"
        onsubmit="return confirm('PERMANENTLY DELETE {{ client.client_name }} and all its tokens? This cannot be undone.')">
    <button type="submit" class="btn btn-danger">
      <i class="ph-light ph-trash"></i> Permanently Delete Client
    </button>
  </form>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 2: Verify in browser**

Open a client detail page — confirm amber secret box, teal rotate zone, dark red danger zone. Check buttons have correct icons.

- [ ] **Step 3: Commit**

```bash
git add src/admin/templates/client_detail.html
git commit -m "feat: rebrand client detail — secret box, zones, icons"
```

---

## Task 5: Update client_create.html and client_edit.html

Info card tokens; add button icons.

**Files:**
- Modify: `src/admin/templates/client_create.html`
- Modify: `src/admin/templates/client_edit.html`

- [ ] **Step 1: Replace client_create.html**

```html
{% extends "base.html" %}
{% block title %}New Client — DS-MOZ MCP OAuth{% endblock %}

{% block content %}
<div class="page-header">
  <h1>New OAuth Client</h1>
  <a href="/admin/clients/" class="btn btn-secondary">
    <i class="ph-light ph-arrow-left"></i> Back
  </a>
</div>

{% if error %}
<div class="error-box">{{ error }}</div>
{% endif %}

<div class="card">
  <form method="post" action="/admin/clients">
    <div class="form-group">
      <label for="client_name">Client Name *</label>
      <input type="text" id="client_name" name="client_name" required
             placeholder="e.g. My MCP Server">
    </div>

    <div class="form-group">
      <label for="redirect_uris_raw">Redirect URIs</label>
      <textarea id="redirect_uris_raw" name="redirect_uris_raw"
                placeholder="One URI per line&#10;Leave empty to allow any redirect URI (development only)"></textarea>
      <small>Leave empty to allow any redirect URI. In production, specify exact URIs.</small>
    </div>

    <div class="form-group">
      <label for="created_by">Created By</label>
      <input type="text" id="created_by" name="created_by" placeholder="optional — your name or system">
    </div>

    <div style="display: flex; gap: 0.75rem; margin-top: 1.5rem;">
      <button type="submit" class="btn btn-primary">
        <i class="ph-light ph-plus"></i> Create Client
      </button>
      <a href="/admin/clients/" class="btn btn-secondary">Cancel</a>
    </div>
  </form>
</div>

<div class="card" style="border-color: var(--focus);">
  <h2 style="color: var(--info-fg); font-size: 0.875rem; margin-bottom: 0.75rem;">After Creation</h2>
  <p style="color: var(--text-muted); font-size: 0.8125rem; line-height: 1.6;">
    The client secret will be shown <strong style="color: var(--text-ui);">once</strong> immediately after creation.
    Copy it immediately — it cannot be recovered.
  </p>
</div>
{% endblock %}
```

- [ ] **Step 2: Replace client_edit.html**

```html
{% extends "base.html" %}
{% block title %}Edit {{ client.client_name }} — DS-MOZ MCP OAuth{% endblock %}

{% block content %}
<div class="page-header">
  <h1>Edit Client</h1>
  <a href="/admin/clients/{{ client.client_id }}" class="btn btn-secondary">
    <i class="ph-light ph-arrow-left"></i> Cancel
  </a>
</div>

<div class="card">
  <form method="post" action="/admin/clients/{{ client.client_id }}/edit">
    <div class="form-group">
      <label for="client_name">Client Name *</label>
      <input type="text" id="client_name" name="client_name" required
             value="{{ client.client_name }}">
    </div>

    <div class="form-group">
      <label for="redirect_uris_raw">Redirect URIs</label>
      <textarea id="redirect_uris_raw" name="redirect_uris_raw"
                placeholder="One URI per line">{{ (client.redirect_uris or []) | join('\n') }}</textarea>
      <small>Leave empty to allow any redirect URI.</small>
    </div>

    <div style="display: flex; gap: 0.75rem; margin-top: 1.5rem;">
      <button type="submit" class="btn btn-primary">
        <i class="ph-light ph-floppy-disk"></i> Save Changes
      </button>
      <a href="/admin/clients/{{ client.client_id }}" class="btn btn-secondary">Cancel</a>
    </div>
  </form>
</div>
{% endblock %}
```

- [ ] **Step 3: Verify in browser**

Open http://localhost:8000/admin/clients/new — info card should have teal border and teal heading. Save and edit forms should have icons.

- [ ] **Step 4: Commit**

```bash
git add src/admin/templates/client_create.html src/admin/templates/client_edit.html
git commit -m "feat: rebrand client create/edit — info card tokens, icons"
```

---

## Task 6: Update client_tokens.html

Add icon to revoke button; update expired badge.

**Files:**
- Modify: `src/admin/templates/client_tokens.html`

- [ ] **Step 1: Replace the file**

```html
{% extends "base.html" %}
{% block title %}{{ client.client_name }} — Tokens — DS-MOZ MCP OAuth{% endblock %}

{% block content %}
<div class="page-header">
  <h1>{{ client.client_name }} — Tokens</h1>
  <a href="/admin/clients/{{ client.client_id }}" class="btn btn-secondary">
    <i class="ph-light ph-arrow-left"></i> Client
  </a>
</div>

{% if tokens %}
<table>
  <thead>
    <tr>
      <th>Fingerprint</th>
      <th>Scopes</th>
      <th>Expires</th>
      <th>Status</th>
      <th>Actions</th>
    </tr>
  </thead>
  <tbody>
    {% for token in tokens %}
    <tr>
      <td style="font-family: monospace; font-size: 0.8rem;">{{ token.fingerprint }}</td>
      <td style="font-size: 0.8rem; color: var(--text-ui);">{{ (token.scopes or []) | join(", ") }}</td>
      <td style="font-size: 0.8rem; color: var(--text-muted);">{{ token.expires_at | unix_to_date }}</td>
      <td>
        {% if token.state == "active" %}
          <span class="badge badge-active">Active</span>
        {% elif token.state == "expired" %}
          <span class="badge" style="background: var(--elevated); color: var(--text-muted);">Expired</span>
        {% else %}
          <span class="badge badge-inactive">Revoked</span>
        {% endif %}
      </td>
      <td>
        {% if token.state == "active" %}
        <form method="post" action="/admin/clients/{{ client.client_id }}/tokens/revoke"
              onsubmit="return confirm('Revoke this token?')">
          <input type="hidden" name="token_hash" value="{{ token.token }}">
          <button type="submit" class="btn btn-danger" style="font-size: 0.75rem; padding: 0.3rem 0.75rem;">
            <i class="ph-light ph-prohibit"></i> Revoke
          </button>
        </form>
        {% else %}
        <span style="color: var(--text-dim); font-size: 0.8rem;">—</span>
        {% endif %}
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<div class="card" style="text-align: center; color: var(--text-muted); padding: 3rem;">
  <p>No tokens issued for this client yet.</p>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 2: Commit**

```bash
git add src/admin/templates/client_tokens.html
git commit -m "feat: rebrand token inspector — icons, token colours"
```

---

## Task 7: Update registrations_list.html and registration_detail.html

Add icons; migrate info card tokens.

**Files:**
- Modify: `src/admin/templates/registrations_list.html`
- Modify: `src/admin/templates/registration_detail.html`

- [ ] **Step 1: Replace registrations_list.html**

```html
{% extends "base.html" %}
{% block title %}Registrations — DS-MOZ MCP OAuth{% endblock %}

{% block content %}
<div class="page-header">
  <h1>Registration Requests</h1>
</div>

{% if registrations %}
<table>
  <thead>
    <tr>
      <th>Company</th>
      <th>Contact</th>
      <th>Email</th>
      <th>Submitted</th>
      <th>Status</th>
      <th>Actions</th>
    </tr>
  </thead>
  <tbody>
    {% for reg in registrations %}
    <tr>
      <td>{{ reg.company_name }}</td>
      <td>{{ reg.contact_name }}</td>
      <td style="font-size: 0.8rem; color: var(--text-ui);">{{ reg.contact_email }}</td>
      <td style="color: var(--text-muted); font-size: 0.8rem;">
        {{ reg.created_at[:10] if reg.created_at else "—" }}
      </td>
      <td>
        {% if reg.status == "pending" %}
          <span class="badge badge-pending">Pending</span>
        {% elif reg.status == "approved" %}
          <span class="badge badge-active">Approved</span>
        {% else %}
          <span class="badge badge-inactive">Rejected</span>
        {% endif %}
      </td>
      <td>
        <a href="/admin/registrations/{{ reg.id }}" class="btn btn-secondary">
          <i class="ph-light ph-eye"></i> Review
        </a>
      </td>
    </tr>
    {% endfor %}
  </tbody>
</table>
{% else %}
<div class="card" style="text-align: center; color: var(--text-muted); padding: 3rem;">
  <p>No registration requests yet.</p>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 2: Replace registration_detail.html**

```html
{% extends "base.html" %}
{% block title %}{{ reg.company_name }} — Registration — DS-MOZ MCP OAuth{% endblock %}

{% block content %}
<div class="page-header">
  <h1>{{ reg.company_name }}</h1>
  <a href="/admin/registrations" class="btn btn-secondary">
    <i class="ph-light ph-arrow-left"></i> Registrations
  </a>
</div>

<div class="card">
  <h2>Request Details</h2>

  <div class="detail-row">
    <span class="detail-label">Status</span>
    <span class="detail-value">
      {% if reg.status == "pending" %}
        <span class="badge badge-pending">Pending</span>
      {% elif reg.status == "approved" %}
        <span class="badge badge-active">Approved</span>
      {% else %}
        <span class="badge badge-inactive">Rejected</span>
      {% endif %}
    </span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Company</span>
    <span class="detail-value" style="font-family: inherit; color: var(--text-h);">{{ reg.company_name }}</span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Contact Name</span>
    <span class="detail-value" style="font-family: inherit; color: var(--text-h);">{{ reg.contact_name }}</span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Contact Email</span>
    <span class="detail-value">{{ reg.contact_email }}</span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Use Case</span>
    <span class="detail-value" style="font-family: inherit; color: var(--text-h); white-space: pre-wrap;">{{ reg.use_case }}</span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Redirect URIs</span>
    <span class="detail-value">
      {% if reg.redirect_uris_raw %}
        {{ reg.redirect_uris_raw }}
      {% else %}
        <span style="color: var(--text-muted); font-family: inherit; font-style: italic;">Not specified</span>
      {% endif %}
    </span>
  </div>

  <div class="detail-row">
    <span class="detail-label">Submitted</span>
    <span class="detail-value" style="font-family: inherit;">{{ reg.created_at or "—" }}</span>
  </div>

  {% if reg.reviewed_at %}
  <div class="detail-row">
    <span class="detail-label">Reviewed</span>
    <span class="detail-value" style="font-family: inherit;">{{ reg.reviewed_at }} by {{ reg.reviewed_by or "admin" }}</span>
  </div>
  {% endif %}
</div>

{% if reg.status == "pending" %}
<div class="card" style="border-color: var(--focus);">
  <h2 style="color: var(--info-fg); margin-bottom: 1rem;">Actions</h2>
  <div style="display: flex; gap: 1rem; flex-wrap: wrap;">
    <form method="post" action="/admin/registrations/{{ reg.id }}/approve">
      <button type="submit" class="btn btn-primary">
        <i class="ph-light ph-check"></i> Approve — Create Client
      </button>
    </form>
    <form method="post" action="/admin/registrations/{{ reg.id }}/reject"
          onsubmit="return confirm('Reject this registration request from {{ reg.company_name }}?')">
      <button type="submit" class="btn btn-danger">
        <i class="ph-light ph-x"></i> Reject
      </button>
    </form>
  </div>
  <p style="color: var(--text-dim); font-size: 0.8rem; margin-top: 1rem; line-height: 1.5;">
    Approving will immediately generate a client ID and secret and redirect you to the client detail page where you can copy the credentials.
  </p>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 3: Commit**

```bash
git add src/admin/templates/registrations_list.html src/admin/templates/registration_detail.html
git commit -m "feat: rebrand registrations — icons, token colours"
```

---

## Task 8: Rewrite consent.html

Full standalone rewrite — light white card on dark background.

**Files:**
- Modify: `src/admin/templates/consent.html`

- [ ] **Step 1: Replace the file**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Grant Access — DS-MOZ MCP OAuth</title>
  <link rel="stylesheet" href="https://unpkg.com/@phosphor-icons/web@2.1.1/src/light/style.css">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Avenir Next', 'Avenir', 'Segoe UI', Helvetica Neue, Arial, sans-serif;
      background: #060E10;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }

    .card {
      background: #ffffff;
      border-radius: 14px;
      padding: 2.5rem;
      max-width: 420px;
      width: 100%;
      box-shadow: 0 16px 56px rgba(0,0,0,0.6);
    }

    .brand {
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: #FF5E00;
      margin-bottom: 2rem;
    }

    h1 {
      font-size: 1.25rem;
      font-weight: 700;
      color: #0A1C20;
      margin-bottom: 0.5rem;
    }

    .client-name {
      font-size: 1.5rem;
      font-weight: 800;
      color: #E8500A;
      margin-bottom: 0.75rem;
    }

    .description {
      font-size: 0.875rem;
      color: #5A8A90;
      line-height: 1.6;
      margin-bottom: 1.5rem;
    }

    .scopes {
      background: #F0F7F8;
      border: 1px solid #D4E8EA;
      border-radius: 8px;
      padding: 1rem 1.25rem;
      margin-bottom: 1.75rem;
    }

    .scopes-label {
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #5A8A90;
      margin-bottom: 0.5rem;
    }

    .scope-item {
      font-size: 0.875rem;
      color: #0A1C20;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    .scope-item::before {
      content: "";
      display: inline-block;
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #3DD68C;
      flex-shrink: 0;
    }

    .form-group { margin-bottom: 1.25rem; }

    label {
      display: block;
      margin-bottom: 0.4rem;
      font-size: 0.8125rem;
      font-weight: 600;
      color: #5A8A90;
    }

    input[type="password"] {
      width: 100%;
      padding: 0.65rem 0.875rem;
      background: #F0F7F8;
      border: 1px solid #D4E8EA;
      border-radius: 6px;
      color: #0A1C20;
      font-size: 0.875rem;
      font-family: inherit;
      outline: none;
      transition: border-color 0.15s;
    }

    input[type="password"]:focus { border-color: #115E67; }

    .error-box {
      background: #FFF0F0;
      border: 1px solid #FFBDBD;
      border-radius: 6px;
      padding: 0.65rem 1rem;
      color: #C0392B;
      font-size: 0.8125rem;
      margin-bottom: 1rem;
    }

    .btn-group { display: flex; gap: 0.75rem; }

    .btn {
      flex: 1;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.375rem;
      padding: 0.65rem 1rem;
      border-radius: 6px;
      font-size: 0.875rem;
      font-weight: 600;
      cursor: pointer;
      border: none;
      transition: opacity 0.15s;
    }

    .btn:hover { opacity: 0.85; }
    .btn i { font-size: 16px; }

    .btn-grant { background: #E8500A; color: #fff; }
    .btn-deny  { background: #F0F7F8; color: #5A8A90; border: 1px solid #D4E8EA; }

    .footer-note {
      margin-top: 1.5rem;
      font-size: 0.75rem;
      color: #91BCC1;
      text-align: center;
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="brand">DS-MOZ Intelligence</div>

    <h1>Grant Access</h1>
    <div class="client-name">{{ client_name }}</div>
    <p class="description">
      This application is requesting access to MCP tools on your behalf.
      Enter your admin password to authorize.
    </p>

    <div class="scopes">
      <div class="scopes-label">Requested Permissions</div>
      {% for scope in scopes %}
      <div class="scope-item">{{ scope }}</div>
      {% endfor %}
    </div>

    {% if error %}
    <div class="error-box">{{ error }}</div>
    {% endif %}

    <form method="post" action="/authorize/consent">
      <input type="hidden" name="session_id" value="{{ session_id }}">

      <div class="form-group">
        <label for="password">Admin Password</label>
        <input type="password" id="password" name="password" autofocus
               placeholder="Enter admin password" required>
      </div>

      <div class="btn-group">
        <button type="submit" class="btn btn-grant">
          <i class="ph-light ph-check"></i> Grant Access
        </button>
        <button type="button" class="btn btn-deny"
                onclick="window.history.back()">
          <i class="ph-light ph-x"></i> Deny
        </button>
      </div>
    </form>

    <p class="footer-note">
      Only authorize applications you trust.<br>
      You can revoke access at any time via the admin panel.
    </p>
  </div>
</body>
</html>
```

- [ ] **Step 2: Verify in browser**

Open http://localhost:8000/authorize/consent?session=test (expect a 422 or redirect, but the page HTML should render). Confirm white card on dark teal background, orange "Grant Access" button.

- [ ] **Step 3: Commit**

```bash
git add src/admin/templates/consent.html
git commit -m "feat: rebrand consent page — light card layout"
```

---

## Task 9: Rewrite consent_waiting.html

Light card, preserve polling JS exactly.

**Files:**
- Modify: `src/admin/templates/consent_waiting.html`

- [ ] **Step 1: Replace the file — keep JS block unchanged**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Waiting for Approval — DS-MOZ MCP OAuth</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Avenir Next', 'Avenir', 'Segoe UI', Helvetica Neue, Arial, sans-serif;
      background: #060E10;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }

    .card {
      background: #ffffff;
      border-radius: 14px;
      padding: 2.5rem;
      max-width: 420px;
      width: 100%;
      box-shadow: 0 16px 56px rgba(0,0,0,0.6);
      text-align: center;
    }

    .brand {
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: #FF5E00;
      margin-bottom: 2rem;
    }

    .spinner {
      width: 48px;
      height: 48px;
      border: 3px solid #D4E8EA;
      border-top-color: #115E67;
      border-radius: 50%;
      animation: spin 0.9s linear infinite;
      margin: 0 auto 1.75rem;
    }

    @keyframes spin { to { transform: rotate(360deg); } }

    h1 {
      font-size: 1.2rem;
      font-weight: 700;
      color: #0A1C20;
      margin-bottom: 0.5rem;
    }

    .client-name {
      font-size: 1.4rem;
      font-weight: 800;
      color: #E8500A;
      margin-bottom: 0.75rem;
    }

    .subtitle {
      font-size: 0.875rem;
      color: #5A8A90;
      line-height: 1.6;
      margin-bottom: 2rem;
    }

    .status-line {
      font-size: 0.8125rem;
      color: #91BCC1;
      margin-top: 1.5rem;
    }

    .error-box {
      display: none;
      background: #FFF0F0;
      border: 1px solid #FFBDBD;
      border-radius: 8px;
      padding: 1rem 1.25rem;
      color: #C0392B;
      font-size: 0.875rem;
      margin-top: 1.5rem;
      line-height: 1.5;
      text-align: left;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="brand">DS-MOZ Intelligence</div>

    <div class="spinner" id="spinner"></div>

    <h1>Waiting for Approval</h1>
    <div class="client-name">{{ client_name }}</div>
    <p class="subtitle">
      A Telegram message has been sent to the owner.<br>
      This page will update automatically once a decision is made.
    </p>

    <div class="status-line" id="status-line">Checking every 2 seconds&hellip;</div>

    <div class="error-box" id="error-box"></div>
  </div>

  <script>
    const SESSION_ID = "{{ session_id }}";
    let attempts = 0;
    const MAX_ATTEMPTS = 150; // 5 min at 2-second intervals

    async function poll() {
      if (attempts >= MAX_ATTEMPTS) {
        showError("Session expired. Please restart the authorization flow.");
        return;
      }
      attempts++;

      try {
        const resp = await fetch(`/consent/status?session=${encodeURIComponent(SESSION_ID)}`);
        if (!resp.ok) {
          scheduleNext();
          return;
        }
        const data = await resp.json();

        if (data.status === "approved") {
          document.getElementById("status-line").textContent = "Approved! Redirecting…";
          if (data.redirect) {
            window.location.href = data.redirect;
          } else {
            showError("Access was granted but no redirect URI was configured for this client.");
          }
          return;
        }

        if (data.status === "denied") {
          showError("Access was denied.");
          return;
        }

        if (data.status === "expired") {
          showError("Session expired. Please restart the authorization flow.");
          return;
        }

        // Still pending — keep polling
        scheduleNext();
      } catch (err) {
        // Network error — keep retrying silently
        scheduleNext();
      }
    }

    function scheduleNext() {
      setTimeout(poll, 2000);
    }

    function showError(msg) {
      document.getElementById("spinner").style.display = "none";
      const box = document.getElementById("error-box");
      box.textContent = msg;
      box.style.display = "block";
      document.getElementById("status-line").textContent = "";
    }

    // Start polling
    scheduleNext();
  </script>
</body>
</html>
```

- [ ] **Step 2: Verify polling still works**

Trigger a real auth flow or check Network tab in DevTools on the consent_waiting page — `/consent/status` requests should fire every 2 seconds.

- [ ] **Step 3: Commit**

```bash
git add src/admin/templates/consent_waiting.html
git commit -m "feat: rebrand consent waiting — light card, teal spinner, preserve polling"
```

---

## Task 10: Rewrite register.html and register_success.html

Light card layout for public registration flow.

**Files:**
- Modify: `src/admin/templates/register.html`
- Modify: `src/admin/templates/register_success.html`

- [ ] **Step 1: Replace register.html**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Request API Access — DS-MOZ MCP OAuth</title>
  <link rel="stylesheet" href="https://unpkg.com/@phosphor-icons/web@2.1.1/src/light/style.css">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Avenir Next', 'Avenir', 'Segoe UI', Helvetica Neue, Arial, sans-serif;
      background: #060E10;
      min-height: 100vh;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      padding: 3rem 1.5rem;
    }

    .card {
      background: #ffffff;
      border-radius: 14px;
      padding: 2.5rem;
      max-width: 520px;
      width: 100%;
      box-shadow: 0 16px 56px rgba(0,0,0,0.6);
    }

    .brand {
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: #FF5E00;
      margin-bottom: 2rem;
    }

    h1 {
      font-size: 1.4rem;
      font-weight: 700;
      color: #0A1C20;
      margin-bottom: 0.4rem;
    }

    .subtitle {
      font-size: 0.875rem;
      color: #5A8A90;
      line-height: 1.6;
      margin-bottom: 2rem;
    }

    .form-group { margin-bottom: 1.25rem; }

    label {
      display: block;
      margin-bottom: 0.4rem;
      font-size: 0.8125rem;
      font-weight: 600;
      color: #5A8A90;
    }

    input[type="text"],
    input[type="email"],
    textarea {
      width: 100%;
      padding: 0.65rem 0.875rem;
      background: #F0F7F8;
      border: 1px solid #D4E8EA;
      border-radius: 6px;
      color: #0A1C20;
      font-size: 0.875rem;
      font-family: inherit;
      outline: none;
      transition: border-color 0.15s;
    }

    input:focus, textarea:focus { border-color: #115E67; }

    textarea { resize: vertical; min-height: 80px; }

    .hint {
      font-size: 0.75rem;
      color: #91BCC1;
      margin-top: 0.3rem;
      display: block;
    }

    .error-box {
      background: #FFF0F0;
      border: 1px solid #FFBDBD;
      border-radius: 6px;
      padding: 0.75rem 1rem;
      color: #C0392B;
      font-size: 0.875rem;
      margin-bottom: 1.25rem;
    }

    .btn-submit {
      width: 100%;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.45rem;
      padding: 0.75rem 1rem;
      background: #E8500A;
      color: #fff;
      border: none;
      border-radius: 6px;
      font-size: 0.9375rem;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      margin-top: 0.5rem;
      transition: opacity 0.15s;
    }

    .btn-submit:hover { opacity: 0.85; }
    .btn-submit i { font-size: 17px; }

    .footer-note {
      margin-top: 1.5rem;
      font-size: 0.75rem;
      color: #91BCC1;
      text-align: center;
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="brand">DS-MOZ Intelligence</div>

    <h1>Request API Access</h1>
    <p class="subtitle">
      Fill in the form below to request access to our MCP services.
      You will receive your credentials by email once the request is reviewed.
    </p>

    {% if error %}
    <div class="error-box">{{ error }}</div>
    {% endif %}

    <form method="post" action="/register/submit">
      <div class="form-group">
        <label for="company_name">Organisation / Company Name *</label>
        <input type="text" id="company_name" name="company_name" required
               placeholder="e.g. LAMBDA Association">
      </div>

      <div class="form-group">
        <label for="contact_name">Contact Name *</label>
        <input type="text" id="contact_name" name="contact_name" required
               placeholder="Your full name">
      </div>

      <div class="form-group">
        <label for="contact_email">Contact Email *</label>
        <input type="email" id="contact_email" name="contact_email" required
               placeholder="you@organisation.org">
      </div>

      <div class="form-group">
        <label for="use_case">Intended Use *</label>
        <textarea id="use_case" name="use_case" required
                  placeholder="Briefly describe how you plan to use the MCP API"></textarea>
      </div>

      <div class="form-group">
        <label for="redirect_uris_raw">Redirect URIs</label>
        <textarea id="redirect_uris_raw" name="redirect_uris_raw"
                  placeholder="One URI per line&#10;e.g. https://myapp.example.com/callback"></textarea>
        <span class="hint">Leave empty if you don't know yet — you can update this later.</span>
      </div>

      <button type="submit" class="btn-submit">
        <i class="ph-light ph-paper-plane-tilt"></i> Submit Request
      </button>
    </form>

    <p class="footer-note">
      Requests are reviewed manually. You will be notified by email once a decision is made.
    </p>
  </div>
</body>
</html>
```

- [ ] **Step 2: Replace register_success.html**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Request Submitted — DS-MOZ MCP OAuth</title>
  <link rel="stylesheet" href="https://unpkg.com/@phosphor-icons/web@2.1.1/src/light/style.css">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Avenir Next', 'Avenir', 'Segoe UI', Helvetica Neue, Arial, sans-serif;
      background: #060E10;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
    }

    .card {
      background: #ffffff;
      border-radius: 14px;
      padding: 2.5rem;
      max-width: 440px;
      width: 100%;
      box-shadow: 0 16px 56px rgba(0,0,0,0.6);
      text-align: center;
    }

    .brand {
      font-size: 0.65rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      color: #FF5E00;
      margin-bottom: 2rem;
    }

    .icon {
      font-size: 3rem;
      color: #3DD68C;
      margin-bottom: 1.25rem;
    }

    h1 {
      font-size: 1.3rem;
      font-weight: 700;
      color: #0A1C20;
      margin-bottom: 0.75rem;
    }

    p {
      font-size: 0.875rem;
      color: #5A8A90;
      line-height: 1.7;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="brand">DS-MOZ Intelligence</div>
    <div class="icon"><i class="ph-light ph-check-circle"></i></div>
    <h1>Request Submitted</h1>
    <p>
      Your access request has been received.<br>
      You will be contacted once it has been reviewed.
    </p>
  </div>
</body>
</html>
```

- [ ] **Step 3: Verify in browser**

Open http://localhost:8000/register — white card, teal inputs on focus, orange submit button with paper-plane icon. Submit a test form to hit the success page — green check circle instead of emoji.

- [ ] **Step 4: Commit**

```bash
git add src/admin/templates/register.html src/admin/templates/register_success.html
git commit -m "feat: rebrand registration pages — light card layout"
```

---

## Task 11: Final verification pass

- [ ] **Step 1: Admin panel full walkthrough**

Visit each admin page and confirm:
- Sidebar visible on desktop (220px), active page has orange left border indicator
- Resize to <768px: sidebar collapses to 40px icon rail, labels hidden, icons remain
- All buttons have icons
- Tables have teal header, correct badge colours (green active, amber pending, muted revoked)
- Stats grid: amber for pending, teal success for active clients

- [ ] **Step 2: Public pages**

- `/register` — white card, light teal inputs, orange submit button
- `/register/success` (POST to submit) — white card, teal check-circle icon
- Consent page (if Telegram not configured, fallback renders) — white card, orange grant button

- [ ] **Step 3: Secret display**

Approve a registration or create a client — secret box should show amber text on dark brown background with key icon label.

- [ ] **Step 4: Polling check**

Open a consent_waiting page. In browser DevTools → Network tab, confirm `/consent/status` requests fire every ~2 seconds.

- [ ] **Step 5: Final commit**

```bash
git add docs/superpowers/specs/2026-04-03-visual-identity-migration-design.md
git add docs/superpowers/plans/2026-04-03-visual-identity-migration.md
git commit -m "docs: add visual identity migration spec and implementation plan"
```
