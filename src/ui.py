"""Shared design system for the Valence web views.

Single source of truth for CSS tokens, component classes, and page shells.
Both ``review`` and ``loading`` import from here instead of duplicating styles.
"""

from __future__ import annotations

from html import escape

# ---------------------------------------------------------------------------
# Design tokens (CSS custom properties) + component classes
# ---------------------------------------------------------------------------

DESIGN_TOKENS = """
:root {
  /* Palette — restrained: neutrals + one accent */
  --color-bg: #f8fafc;
  --color-surface: #ffffff;
  --color-surface-alt: #f1f5f9;
  --color-border: #e2e8f0;
  --color-border-strong: #cbd5e1;

  --color-text: #0f172a;
  --color-text-secondary: #64748b;
  --color-text-muted: #94a3b8;

  --color-accent: #0d9488;
  --color-accent-hover: #0f766e;
  --color-accent-light: #ccfbf1;

  --color-success: #16a34a;
  --color-success-bg: #dcfce7;
  --color-success-text: #166534;

  --color-danger: #dc2626;
  --color-danger-bg: #fee2e2;
  --color-danger-text: #991b1b;

  --color-warning: #d97706;
  --color-warning-bg: #fef3c7;
  --color-warning-text: #92400e;

  --color-info: #4f46e5;
  --color-info-bg: #e0e7ff;
  --color-info-text: #3730a3;

  --color-purple: #7c3aed;
  --color-purple-bg: #f3e8ff;
  --color-purple-text: #6b21a8;

  /* Typography */
  --font-sans: system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace;
  --text-xs: 0.75rem;
  --text-sm: 0.8125rem;
  --text-base: 0.875rem;
  --text-lg: 1rem;
  --text-xl: 1.125rem;
  --text-2xl: 1.375rem;
  --text-3xl: 1.75rem;

  /* Spacing (4px grid) */
  --space-1: 4px;
  --space-2: 8px;
  --space-3: 12px;
  --space-4: 16px;
  --space-5: 20px;
  --space-6: 24px;
  --space-8: 32px;
  --space-10: 40px;
  --space-12: 48px;
  --space-16: 64px;

  /* Radius */
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-full: 9999px;

  /* Shadows */
  --shadow-sm: 0 1px 2px rgba(0,0,0,0.05);
  --shadow-md: 0 1px 3px rgba(0,0,0,0.07), 0 1px 2px rgba(0,0,0,0.04);
  --shadow-lg: 0 4px 6px rgba(0,0,0,0.05), 0 2px 4px rgba(0,0,0,0.03);

  /* Motion */
  --transition-fast: 120ms ease-out;
  --transition-base: 180ms ease-out;
}
"""

COMPONENT_CSS = """
/* ---- Reset & base ---- */
*, *::before, *::after { box-sizing: border-box; }
body {
  font-family: var(--font-sans);
  font-size: var(--text-base);
  line-height: 1.6;
  margin: 0;
  background: var(--color-bg);
  color: var(--color-text);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
a { color: var(--color-accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ---- Layout ---- */
.wrap { max-width: 960px; margin: 0 auto; padding: var(--space-6) var(--space-5) var(--space-16); }
header {
  background: var(--color-text);
  color: #f8fafc;
  padding: var(--space-3) var(--space-5);
  display: flex;
  align-items: center;
  gap: var(--space-5);
}
header .brand {
  font-weight: 700;
  font-size: var(--text-lg);
  color: #ffffff;
  letter-spacing: -0.01em;
}
header nav { display: flex; gap: var(--space-4); align-items: center; }
header nav a {
  color: #94a3b8;
  font-size: var(--text-sm);
  font-weight: 500;
  transition: color var(--transition-fast);
}
header nav a:hover { color: #ffffff; text-decoration: none; }
header form { margin-left: auto; }

/* ---- Typography ---- */
h1 {
  font-size: var(--text-2xl);
  font-weight: 700;
  margin: 0 0 var(--space-2);
  letter-spacing: -0.02em;
  color: var(--color-text);
}
h2 {
  font-size: var(--text-lg);
  font-weight: 600;
  margin: var(--space-6) 0 var(--space-3);
  color: var(--color-text);
}
h2:first-child { margin-top: 0; }
.sub {
  color: var(--color-text-secondary);
  margin: 0 0 var(--space-5);
  font-size: var(--text-sm);
}

/* ---- Cards ---- */
.card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-5);
  margin-bottom: var(--space-4);
  box-shadow: var(--shadow-sm);
}

/* ---- Stat bar ---- */
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: var(--space-3);
  margin-bottom: var(--space-5);
}
.stat {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-4) var(--space-5);
  box-shadow: var(--shadow-sm);
}
.stat .n {
  font-size: var(--text-3xl);
  font-weight: 700;
  letter-spacing: -0.03em;
  line-height: 1.1;
  color: var(--color-text);
}
.stat .l {
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  margin-top: var(--space-1);
}

/* ---- Badges ---- */
.badges { margin-top: var(--space-2); display: flex; flex-wrap: wrap; gap: var(--space-1); }
.badge {
  display: inline-flex;
  align-items: center;
  border-radius: var(--radius-full);
  padding: 2px var(--space-3);
  font-size: var(--text-xs);
  font-weight: 600;
  line-height: 1.6;
  white-space: nowrap;
}
.badge.b { background: var(--color-danger-bg); color: var(--color-danger-text); }
.badge.g { background: var(--color-success-bg); color: var(--color-success-text); }
.badge.n { background: var(--color-info-bg); color: var(--color-info-text); }
.badge.d { background: var(--color-purple-bg); color: var(--color-purple-text); }
.badge.warning { background: var(--color-warning-bg); color: var(--color-warning-text); }

/* ---- Buttons ---- */
.btn, button {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border-strong);
  background: var(--color-surface);
  color: var(--color-text);
  padding: var(--space-2) var(--space-4);
  font-size: var(--text-sm);
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all var(--transition-fast);
  text-decoration: none;
  line-height: 1.4;
}
.btn:hover, button:hover {
  background: var(--color-surface-alt);
  border-color: var(--color-text-muted);
  text-decoration: none;
}
button.approve, .btn-primary {
  background: var(--color-success);
  border-color: var(--color-success);
  color: #ffffff;
}
button.approve:hover, .btn-primary:hover {
  background: #15803d;
  border-color: #15803d;
}
button.reject, .btn-danger {
  background: var(--color-danger);
  border-color: var(--color-danger);
  color: #ffffff;
}
button.reject:hover, .btn-danger:hover {
  background: #b91c1c;
  border-color: #b91c1c;
}
button.dispatch {
  background: var(--color-warning);
  border-color: var(--color-warning);
  color: #ffffff;
}
button.dispatch:hover {
  background: #b45309;
  border-color: #b45309;
}
.btn-ghost {
  background: transparent;
  border-color: transparent;
  color: var(--color-text-secondary);
}
.btn-ghost:hover {
  background: var(--color-surface-alt);
  color: var(--color-text);
}
form.inline { display: inline; }

/* ---- Forms ---- */
input[type=text], input[type=password], input[type=number], select {
  border: 1px solid var(--color-border-strong);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  font-family: inherit;
  color: var(--color-text);
  background: var(--color-surface);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
  width: 100%;
  max-width: 100%;
}
input:focus, select:focus {
  outline: none;
  border-color: var(--color-accent);
  box-shadow: 0 0 0 3px var(--color-accent-light);
}
.searchbar {
  display: flex;
  gap: var(--space-2);
  margin-bottom: var(--space-5);
}
.searchbar input { flex: 1; }

/* ---- Alerts ---- */
.error {
  background: var(--color-danger-bg);
  color: var(--color-danger-text);
  border: 1px solid #fecaca;
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-4);
  font-size: var(--text-sm);
}
.notice {
  background: var(--color-success-bg);
  color: var(--color-success-text);
  border: 1px solid #bbf7d0;
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-4);
  font-size: var(--text-sm);
}

/* ---- Tables ---- */
table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}
th, td {
  text-align: left;
  padding: var(--space-2) var(--space-3);
  vertical-align: top;
}
th {
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  border-bottom: 1px solid var(--color-border);
}
td { border-top: 1px solid var(--color-surface-alt); }
tr:hover td { background: var(--color-surface-alt); }

/* ---- Definition list (detail pages) ---- */
dl.fields {
  display: grid;
  grid-template-columns: 140px 1fr;
  gap: var(--space-2) var(--space-4);
  font-size: var(--text-sm);
}
dl.fields dt {
  color: var(--color-text-secondary);
  font-weight: 600;
  padding: var(--space-1) 0;
}
dl.fields dd {
  margin: 0;
  padding: var(--space-1) 0;
}

/* ---- Order row (queue) ---- */
.order-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: var(--space-3);
}
.order-row .id {
  font-weight: 600;
  color: var(--color-text);
  font-size: var(--text-base);
}
.meta {
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  margin-top: var(--space-1);
}

/* ---- Login card ---- */
.login-card {
  max-width: 380px;
  margin: var(--space-16) auto;
}
.login-card h1 { text-align: center; }

/* ---- Edit table ---- */
.edit-items {
  width: 100%;
  border-collapse: collapse;
}
.edit-items th {
  text-align: left;
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  padding: var(--space-1) var(--space-2);
}
.edit-items td {
  padding: var(--space-2) var(--space-2) var(--space-2) 0;
}
.edit-items input, .edit-items select {
  width: 100%;
  box-sizing: border-box;
}

/* ---- Empty state ---- */
.empty-state {
  text-align: center;
  padding: var(--space-10) var(--space-5);
  color: var(--color-text-muted);
}
.empty-state p { margin: 0; }

/* ---- Transcription block ---- */
.transcription {
  background: var(--color-surface-alt);
  border-left: 3px solid var(--color-accent);
  padding: var(--space-3) var(--space-4);
  border-radius: 0 var(--radius-md) var(--radius-md) 0;
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  margin-top: var(--space-3);
  font-style: italic;
}

/* ---- Print styles ---- */
@media print {
  body { background: #ffffff; }
  header, .no-print { display: none; }
  .wrap { max-width: none; padding: 0; }
  .card { border: 1px solid #000000; border-radius: 0; box-shadow: none; }
  button, .btn { display: none; }
  a { color: inherit; }
  tr:hover td { background: transparent; }
}
"""

# ---------------------------------------------------------------------------
# Page shell helpers
# ---------------------------------------------------------------------------


def page_shell(title: str, body: str, *, nav_links: str = "", active: str = "") -> str:
    """Full HTML document shell used by review and loading views."""
    nav = nav_links or (
        "<a href='/review'>Review</a>"
        "<a href='/review/orders'>All orders</a>"
    )
    return (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} — Valence</title>"
        f"<style>{DESIGN_TOKENS}{COMPONENT_CSS}</style>"
        "</head><body>"
        "<header>"
        "<a href='/' class='brand'>Valence</a>"
        f"<nav>{nav}</nav>"
        "<form method='post' action='/review/logout'>"
        "<button class='btn-ghost' type='submit' style='color:#94a3b8;border-color:transparent'>"
        "Log out</button></form>"
        "</header>"
        f"<div class='wrap'>{body}</div>"
        "</body></html>"
    )


def login_page_shell(
    title: str,
    heading: str,
    subtitle: str,
    action: str,
    error: str | None = None,
    nav_links: str = "",
) -> str:
    """Login page shell — centered card with passcode field."""
    err = f"<div class='error'>{escape(error)}</div>" if error else ""
    body = (
        f"<div class='login-card card'>"
        f"<h1>{escape(heading)}</h1>"
        f"<p class='sub' style='text-align:center'>{escape(subtitle)}</p>"
        f"{err}"
        f"<form method='post' action='{escape(action)}'>"
        f"<input type='password' name='passcode' placeholder='Passcode' "
        f"autocomplete='current-password' required> "
        f"<button class='btn-primary' type='submit' "
        f"style='width:100%;margin-top:var(--space-3);justify-content:center'>"
        f"Enter</button></form></div>"
    )
    return (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)} — Valence</title>"
        f"<style>{DESIGN_TOKENS}{COMPONENT_CSS}</style>"
        "</head><body>"
        f"<div class='wrap' style='max-width:420px'>{body}</div>"
        "</body></html>"
    )
