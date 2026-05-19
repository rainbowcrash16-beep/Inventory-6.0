# Inventory-6.0

A pair of dark-themed, mobile-first PWAs that share a visual language.

## Apps

- **[MyStuff — Home Tracker](./index.html)** (`index.html`)
  Inventory of things in your home: where each item lives, who borrowed it, what's broken or empty, with QR labels and a scanner.

- **[Kanban — Tasks](./kanban.html)** (`kanban.html`)
  A four-column board (Backlog / To Do / Doing / Done) for things that need to get done. Tasks carry an Urgent/High/Medium/Low priority and auto-sort within each column. Drag cards between columns on desktop or mobile, or tap a card to use move buttons.

## Running the frontend

**Offline / static**: open either HTML file directly in a browser, or serve the folder with any static file server. Data persists in your browser's `localStorage`. The two apps use separate storage keys.

## Running the Kanban backend (optional)

The Kanban app can also run against a Python backend (`server/`) that syncs your board with **Google Tasks**, **Google Calendar**, and **Gmail** (auto-create tasks from labeled emails + suggest tasks from your inbox).

See **[`server/SETUP.md`](./server/SETUP.md)** for a step-by-step deployment guide using Render (free) + Neon Postgres (free) + Google OAuth.

```
Browser (kanban.html)
   │  fetch('/api/...')
   ▼
Flask server (server/app.py)  ──▶  SQLite / Postgres
                              ──▶  Google Tasks  (push tasks)
                              ──▶  Google Calendar (events for due dates)
                              ──▶  Gmail (scan inbox, suggest tasks)
```

The frontend auto-detects whether a backend is reachable: if it is, it uses the API; otherwise it falls back to `localStorage` and hides the sync UI. Same HTML file, two modes.
