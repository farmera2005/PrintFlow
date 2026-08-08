# PrintFlow

Order-orchestration platform for a 3D printing business. It does not replace
Etsy, QuickBooks Online, Bambuddy or ShipStation — it is the connective tissue
between them and the single pane of glass over the whole fulfilment flow.

| Platform           | Role                                        | Direction                       |
| ------------------ | ------------------------------------------- | ------------------------------- |
| Etsy               | Sales channel — source of orders            | Read only (poll receipts)       |
| QuickBooks Online  | Inventory system of record — quantity on hand | Read only (item quantities)   |
| Bambuddy           | Print farm manager — queue + archived 3MFs  | Read/write (queue, track)       |
| ShipStation        | Shipping — labels, tracking pushback to Etsy | Read/write (match, buy labels) |

PrintFlow itself owns the order state machine, the flow board, the bundle
(BOM) definitions and the SKU → print-file mappings.

## The workflow

1. An order arrives from Etsy and is polled in.
2. Line items are matched to products by SKU (case-insensitive, trimmed).
3. Bundle SKUs explode into component SKUs using the BOM table.
4. For each component, QuickBooks quantity on hand decides print-or-pull:
   stock covers the need → allocate; shortfall → print the shortfall only.
5. Print jobs go to the Bambuddy queue — one queue item per plate, with the
   plate count derived from `ceil(qty_to_print / units_per_plate)`.
6. Bambuddy job completion advances the cards on the kanban board.
7. At **Ready to Ship**, you click Create Label. ShipStation's own Etsy
   connection pushes the tracking number back to Etsy.

**PrintFlow never writes to Etsy or QuickBooks.** Labels are never bought
automatically — they cost money, so they are always an explicit click.

## Running it

Two services, nothing to edit. Designed to sit on a NAS next to the existing
Bambuddy container.

```bash
git clone <this repo> && cd PrintFlow
docker compose up -d
```

Then open **`https://<your-nas>:8443`** and work through the setup wizard.

That is the whole install. There is no file to edit, no secret to invent and no
certificate to generate by hand:

- **The encryption key** is generated on first boot and kept in the
  `printflow-data` volume. Set `SECRET_KEY` yourself only if you would rather
  manage it — but do not change it later, because stored credentials are
  encrypted with it. (If it ever does change, the app says so plainly and asks
  you to reconnect each integration rather than failing obscurely.)
- **The HTTPS certificate** is generated on first boot too, so the wizard is
  already on https. It is self-signed, so your browser warns once — the wizard
  re-issues it for your real hostname and offers it for download so you can
  trust it.
- **Port 8000** (published as 8420) stays open on plain HTTP as a way back in
  if a certificate ever goes wrong.

`DATABASE_URL` and `SECRET_KEY` are the only environment variables that mean
anything, and both are optional in the compose file. There are no config files
and no seed scripts — **every** setting is entered in the app.

### First-run setup wizard

1. **Admin account** — username + password (bcrypt, session cookie).
2. **Access & security** — the address you reach PrintFlow on, and the HTTPS
   certificate: re-issue it for your hostnames and IPs, download it for your
   trust store, or upload your own certificate and key. Optionally redirect
   plain HTTP to HTTPS. This step comes before the OAuth steps because Etsy and
   Intuit both reject an `http://` redirect URI.
3. **Etsy** — paste your app's keystring and shared secret; the app runs the
   OAuth 2.0 PKCE flow and you pick the shop. Refresh tokens rotate and the new
   one is persisted on every refresh.
4. **QuickBooks Online** — paste your Intuit app's client ID and secret; the
   app runs the OAuth flow and stores the realm ID. Access tokens refresh
   silently.
5. **Bambuddy** — base URL + API key. Validated by fetching the instance's
   OpenAPI document (version is logged) and listing the printers it finds.
6. **ShipStation** — API key + secret. Validated by listing stores; you pick
   the one that receives your Etsy orders.
7. **Poll intervals** — defaults pre-filled (Etsy 5 min, Bambuddy 2 min,
   ShipStation 10 min).

The Etsy and QuickBooks steps show the exact redirect URI to register, built
from the address set in step 2 — so set that first if you reach PrintFlow
through a reverse proxy or a hostname other than the one it guesses.

Every integration is reconfigurable later from **Settings**, including full
re-auth, and the wizard can be re-opened at any time.

### About the certificate

Re-issuing applies immediately: the HTTPS listener restarts on the new
certificate without a container restart. The swap is deferred until just after
the response is sent, because that response is being served over the listener
being replaced. Your browser will ask you to trust the new certificate.

If you would rather terminate TLS at a reverse proxy, point it at port 8000,
set **Public base URL** to the proxy's address and ignore 8443.

### Backups

Two volumes matter: `printflow-db` (all the data) and `printflow-data` (the
encryption key and the certificate). Without the key, stored credentials cannot
be decrypted — back it up alongside the database.

## The board

`New → In Production → Assembly → Ready to Ship → Shipped`

Cards move because the data moved — dragging is deliberately disabled. Manual
overrides live in the card menu (mark a line printed for an off-Bambuddy
print, skip stock and print anyway, cancel a line) and **every override writes
an audit row**.

- **New** — ingested, decisions made, nothing dispatched yet. Orders with an
  unmatched SKU stay here with a red badge so the fix path stays visible; the
  fix is "link product" from the order drawer, which re-runs intake for that
  line.
- **In Production** — at least one line dispatched to Bambuddy. A failed plate
  holds the line here and flags it red with a one-click Re-queue.
- **Assembly** — production finished but the order contains a bundle, which
  needs a manual check-off per bundle. Orders without bundles skip this column.
- **Ready to Ship** — every line ready. The Create Label button goes live.
- **Shipped** — label bought, tracking shown.

Other screens: **Products** (CRUD, QBO item picker, Bambuddy archive browser,
BOM editor), **Print Queue** (every plate across all orders, re-queue/cancel),
**Sync Log** (background runs + audit trail), **Settings**.

## How the decisioning works

For each component line:

```
available      = QtyOnHand - (stock already claimed by other open lines)
qty_from_stock = min(quantity, max(available, 0))
qty_to_print   = quantity - qty_from_stock
```

That soft-reservation subtraction is what stops two simultaneous orders from
both claiming the same last unit. A reservation is released when its line ships
or is cancelled.

A `printed` product with no QuickBooks item linked skips the stock check
entirely and prints in full. A `stocked` product is always allocated from
stock and warns on the card if QuickBooks says there is not enough. If
QuickBooks is unreachable, printed lines fall back to printing the full
quantity and say so on the line — intake never blocks on a third party.

## Background jobs

| Job                        | Default   | Notes                                    |
| -------------------------- | --------- | ---------------------------------------- |
| Etsy receipt poll          | 5 min     | Intake pipeline runs inline after ingest |
| Bambuddy status reconcile  | 2 min     | Dispatches pending plates, then advances jobs |
| ShipStation order match    | 10 min    | Only orders missing a ShipStation id; backs off because ShipStation's Etsy import can lag an hour |
| QBO token refresh          | 5 min check | Refreshes at <10 min remaining         |

Every run writes to `sync_log`. Integration failures never crash the app: they
raise a persistent banner naming the integration and the error, with a
**Retry now** button that runs that job immediately. All external calls have
timeouts and bounded retries with exponential backoff, and every pipeline step
is idempotent — re-running intake on a processed receipt is a no-op.

## Bambuddy endpoint configuration

Bambuddy is self-hosted and its schema can move between releases, so the
endpoint paths and the queue payload field names are stored with the
credentials and editable under **Settings → Bambuddy → Advanced**. Defaults:

```json
{ "paths":  { "openapi": "/openapi.json", "printers": "/api/printers",
              "archives": "/api/archives", "queue": "/api/queue" },
  "fields": { "archive_id": "archive_id", "plate_number": "plate",
              "printer_id": "printer_id" } }
```

Responses are parsed defensively (several common field spellings and envelope
shapes are accepted), so a renamed field degrades to "unknown" rather than
breaking a poll. A print mapping's `print_options` JSON is merged into the
queue request as-is.

## Development

```bash
# Backend (needs a PostgreSQL 16 on hand)
python -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
export DATABASE_URL=postgresql+asyncpg://printflow@localhost:5432/printflow
export PRINTFLOW_DATA_DIR=./.printflow-data   # optional; this is the default
cd backend && alembic upgrade head

python -m app.server        # HTTP on 8000 + HTTPS on 8443, as in production
uvicorn app.main:app --reload   # or HTTP only, with reload

# Frontend (proxies /api to :8000)
cd frontend && npm install && npm run dev
```

Under plain `uvicorn` there is no TLS supervisor, so saving a certificate says
"restart to apply" rather than pretending it hot-swapped.

### Tests

```bash
createdb printflow_test
cd backend && pytest              # 186 tests
```

The suite covers the state machine and decisioning maths as pure functions, the
full intake pipeline against a real PostgreSQL (bundle explosion, soft
reservations, plate planning, dispatch, reconcile, assembly gate, failure and
re-queue), the HTTP API including the setup wizard and label purchase, the
integration clients (credential encryption, PKCE, retry/backoff, rate limiting,
response parsing), and the security bootstrap (secret-key generation and
persistence, certificate generation and validation, the security endpoints).

`TEST_DATABASE_URL` overrides the test database.

### Layout

```
backend/app/
  config.py            data dir + secret key bootstrap
  server.py            HTTP + HTTPS listeners, hot certificate reload
  models.py            schema, state vocabularies
  services/
    tls.py             certificate generation, validation, storage
    intake.py          receipt → lines → bundle explosion
    allocation.py      QBO print-or-pull decisioning
    printing.py        plate maths, dispatch, reconcile
    shipping.py        ShipStation matching + labels
    state.py           line state machine + order roll-up (pure functions)
    board.py           read models for the board and drawer
  integrations/        etsy, qbo, bambuddy, shipstation clients
  routers/             HTTP API
  scheduler.py         APScheduler jobs
frontend/src/          React + Vite + Tailwind SPA
```

## Not in v1

No writes to Etsy or QuickBooks. No accounting features. No multi-user roles.
No multi-level BOMs (a bundle may not contain a bundle — this is validated). No
automatic label purchase. No filament or spool tracking — Bambuddy owns that.
