# Inventory-6.0

A pair of dark-themed, mobile-first PWAs that share a visual language. Each is a single, self-contained HTML file — no build step, no framework, no server.

## Apps

- **[MyStuff — Home Tracker](./index.html)** (`index.html`)
  Inventory of things in your home: where each item lives, who borrowed it, what's broken or empty, with QR labels and a scanner.

- **[Kanban — Tasks](./kanban.html)** (`kanban.html`)
  A four-column board (Backlog / To Do / Doing / Done) for things that need to get done. Tasks carry an Urgent/High/Medium/Low priority and auto-sort within each column. Drag cards between columns on desktop or mobile, or tap a card to use move buttons.

## Running

Open either file directly in a browser, or serve the folder with any static file server. Data is stored locally in your browser (`localStorage`); the two apps use separate storage keys and do not share data.
