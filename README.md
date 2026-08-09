# PrintFlow

Order-orchestration platform for a 3D printing business. It does not replace
Etsy, QuickBooks Online, Bambuddy or ShipStation — it is the connective tissue
between them and the single pane of glass over the whole fulfilment flow.

| Platform           | Role                                        | Direction                       |
| ------------------ | ------------------------------------------- | ------------------------------- |
| Etsy               | Sales channel — source of orders            | Read only (poll receipts)       |
| QuickBooks Online  | Inventory system of record — quantity on hand | Read for orders; writes only a made-items sheet you post |
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

**PrintFlow never writes to Etsy.** Order handling never writes to QuickBooks
either — importing, deciding and fulfilling an order only ever reads quantity on
hand. The single exception is the Manufacturing tab, and it only acts when you
press Post. Labels are never bought automatically — they cost money, so they are
always an explicit click.

## Manufacturing (made-items sheets)

When you make items into stock rather than against an order, record it on the
**Manufacturing** tab. A sheet lists what you made and at what unit cost;
posting it writes **one QuickBooks Expense** carrying:

* a positive item line per product made — quantity on hand goes **up**;
* a negative item line per component its BOM consumed — quantity on hand goes
  **down**.

Costed at the BOM roll-up, the two sides cancel and the expense totals zero: the
sheet simply moves value out of components and into finished goods. Type a
higher unit cost to absorb labour or machine time and the difference lands in
the account you choose under **Settings → QuickBooks → Manufacturing postings**.
There is no default for that account, and posting stays blocked until you pick
one — where manufacturing cost belongs depends on your chart of accounts, and
guessing would file it somewhere you did not choose.

Guardrails, because this is the one place PrintFlow writes to your books:

* Nothing posts without a person pressing Post. No background job posts.
* The sheet shows exactly what will happen — both totals and the difference —
  before you commit, and again in the confirmation.
* A posted sheet is immutable. Correcting one means Void (which deletes the
  QuickBooks transaction and reverses every quantity) plus a new sheet.
* Each posting carries an idempotency key, so a timeout followed by a retry
  cannot produce two transactions.
* A product with no QuickBooks item linked blocks the post rather than silently
  posting a sheet that moves nothing.

## Running it

Two services, nothing to edit. Designed to sit on a NAS next to the existing
Bambuddy container.

```bash
git clone https://github.com/farmera2005/PrintFlow.git
cd PrintFlow
docker compose up -d
```

Then open **`https://<your-nas>:8443`** and work through the setup wizard.

Clone it with `git`, not as a zip — updating later is a `git pull`, and that
needs a real checkout.

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
2. **Access & security** — the address you reach PrintFlow on, the HTTPS
   certificate (re-issue it for your hostnames and IPs, download it for your
   trust store, or upload your own), and the Cloudflare Tunnel. This step comes
   before the OAuth steps because Etsy and Intuit both reject an `http://`
   redirect URI, and Intuit will not accept a private address at all for a
   production app.
3. **Etsy** — paste your app's keystring and shared secret; the app runs the
   OAuth 2.0 PKCE flow and you pick the shop. Both halves of the credential are
   required: Etsy's API wants `x-api-key: <keystring>:<shared_secret>`, and
   answers 403 to either one on its own. Refresh tokens rotate and the new one
   is persisted on every refresh. Picking the shop sets an import cutoff of
   "now", so an established shop's open back catalogue does not land on the
   board; move it back on the Etsy panel to backfill.
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

### Public callback URLs (Cloudflare Tunnel)

Etsy and QuickBooks send the browser back to PrintFlow after you authorise
them, and Intuit rejects a private address for a production app. A Cloudflare
Tunnel solves that without opening a single inbound port — `cloudflared` makes
only outbound connections — and it is configured in the wizard like everything
else. The binary is bundled in the image and PrintFlow supervises it as a child
process, so there is no second container to wire up.

**Named tunnel** (the one to use). In Cloudflare Zero Trust → Networks →
Tunnels, create a tunnel, add a public hostname routed to
`http://localhost:8000`, and paste the connector token into the wizard along
with the hostname.

The callback URLs follow the tunnel automatically. Once it is configured, its
hostname *is* PrintFlow's public address, and **Access & security → Address &
callback URLs** shows the two URIs to register verbatim:

```
https://printflow.example.com/api/integrations/etsy/callback
https://printflow.example.com/api/integrations/qbo/callback
```

That precedence is deliberate. A redirect URI has to match what you registered
character for character, and the alternative — deriving it from whichever
address you happen to be browsing — means opening PrintFlow on the LAN and
clicking Connect sends Etsy a `https://192.168.1.50:8443/...` redirect that was
never registered. The tunnel hostname wins over the manual **Public base URL**
for the same reason, so a stale value left over from before the tunnel cannot
quietly break authorisation. Turn the tunnel off and the manual value applies
again.

**Quick tunnel** is a throwaway `*.trycloudflare.com` address needing no
account. It is genuinely useful for testing the OAuth round-trip and genuinely
unsuitable for anything else: Cloudflare assigns a new hostname on every
restart, so a redirect URI registered against it stops working.

The token is encrypted at rest and passed to `cloudflared` through the
environment, never as a command-line argument — argv is world-readable in `ps`.
It is never echoed back by the API and is redacted from the captured log the UI
shows. The tunnel restarts itself with backoff if `cloudflared` dies, and
starts automatically on boot from the stored configuration.

Two things worth knowing before you expose the app:

- **Put Cloudflare Access in front of it.** A tunnel makes the login page
  reachable from the internet. An Access policy on the hostname means visitors
  authenticate at Cloudflare's edge before a request ever reaches PrintFlow.
  PrintFlow's own single-admin login still applies underneath.
- Cloudflare terminates TLS at the edge and forwards over plain HTTP to port
  8000, which is why the tunnel points there rather than at 8443 — tunnelling to
  the self-signed port would only add a certificate for Cloudflare to distrust.
  The session cookie is marked `Secure` automatically whenever the request
  arrives over HTTPS, which through a tunnel it always does.

If the image was built without network access the binary will be missing; the
UI says so plainly and you can run `cloudflared` yourself against
`http://<host>:8000` instead.

### "502 Bad gateway" on the OAuth callback

You finish authorising on Etsy or Intuit, get redirected back, and land on a
Cloudflare error page. Read its three status blocks: Browser ✓, Cloudflare ✓,
**Host ✗**. Nothing failed at Etsy — Cloudflare could not reach PrintFlow.

Almost always the tunnel's Public Hostname points at the wrong local address.
In the Cloudflare dashboard the **Service** must be:

```
HTTP    localhost:8000
```

Plain HTTP, port 8000. Two ways to get this wrong:

- **`HTTPS` / port 8443.** That port serves the self-signed certificate, which
  Cloudflare will not trust — 502. Cloudflare terminates TLS at the edge, so
  the hop to PrintFlow is plain HTTP by design.
- **`localhost` when cloudflared runs in its own container.** Then `localhost`
  is that container, not PrintFlow. Use `http://app:8000`.

**Access & security → Public access → Test public URL** fetches your own public
URL from the server and names the broken hop: a working origin, a 502 with the
service value to fix, a missing connector, or a Cloudflare Access policy (which
blocks the server-side probe but is harmless — browsers still work).

### "502 Bad gateway" from one slow page, while the rest of the app works

Same Cloudflare page, different cause. Cloudflare stops waiting for the origin
at 100 seconds; if a single request takes longer, it answers with its own error
page — and that page names *your* hostname, so it reads like the service you
were configuring is at fault when it is not.

The wrong-Bambuddy-address case used to take 3m32s (four OpenAPI probes and
three printer attempts, each willing to wait out a 30s read timeout) and so
always turned into a 502. Bambuddy calls now use LAN-appropriate timeouts,
the OpenAPI probe stops as soon as the host proves unreachable, and setup
validation runs under a 20-second budget, so you get PrintFlow's own message —
which names the address it actually tried.

If you do see a proxy error page, PrintFlow now condenses it to one line saying
where it came from rather than pasting the markup into the panel.

### About the certificate

Re-issuing applies immediately: the HTTPS listener restarts on the new
certificate without a container restart. The swap is deferred until just after
the response is sent, because that response is being served over the listener
being replaced. Your browser will ask you to trust the new certificate.

If you would rather terminate TLS at a reverse proxy, point it at port 8000,
set **Public base URL** to the proxy's address and ignore 8443.

## Updating

### Is my update actually live?

```bash
curl -sk https://localhost:8443/api/health
```

```json
{"ok":true,"version":"5b63a74","features":{"cloudflare_tunnel":true,"cloudflared_installed":true}}
```

No login needed. `version` is the commit the running container was built from.
If it is missing, or `features` is absent entirely, the container predates that
build — `docker compose up -d` on its own does **not** rebuild, you need
`--build` (which `./update.sh` does for you).

If the version looks right but the UI still looks old, it is your browser:
hard-refresh with **Ctrl/Cmd + Shift + R**. Builds from `5b63a74` onwards send
`Cache-Control: no-cache` on `index.html` so this cannot happen again.


```bash
cd PrintFlow
./update.sh
```

That pulls, rebuilds and restarts. Or by hand:

```bash
git pull && docker compose up -d --build
```

Nothing else is needed. Database migrations run automatically when the
container starts, and your data lives in Docker volumes rather than in the
image, so a rebuild does not touch it. **Settings → Version** shows the running
commit, which is the quickest way to confirm an update actually reached the
container.

### Keep your local tweaks out of git's way

The one thing that breaks pull-based updates is editing a tracked file. Nothing
in a normal install requires it.

**Changing the published ports** (if 8443 or 8420 are taken on your NAS): put
them in `.env`, which Compose loads automatically and git ignores.

```bash
echo "PRINTFLOW_HTTPS_HOST_PORT=9443" >> .env
echo "PRINTFLOW_HTTP_HOST_PORT=9420"  >> .env
docker compose up -d
```

**Anything else** — an extra volume, a timezone, your own `SECRET_KEY` — goes in
`docker-compose.override.yml`, which Compose merges over the tracked file and
git also ignores:

```bash
cp docker-compose.override.yml.example docker-compose.override.yml
```

Ports are handled through `.env` rather than the override file on purpose:
Compose merges list fields such as `ports` by *appending*, so overriding them
there publishes both the old and the new port and fails on the very conflict you
were avoiding. Compose 2.24+ can force a replace with `ports: !override [...]`,
but older versions — including some NAS packages — cannot.

`update.sh` refuses to run if tracked files have been modified, and says which
ones, rather than leaving a half-updated checkout behind.

### Before a big update

Both volumes are worth a snapshot first:

```bash
docker run --rm -v printflow-db:/db -v printflow-data:/data -v "$PWD":/backup \
  alpine tar czf /backup/printflow-backup.tar.gz /db /data
```

Migrations are forward-only; there is no automatic downgrade path.

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

Bambuddy is self-hosted, ships hundreds of endpoints, and moves them between
releases — so PrintFlow does not assume where they are. On every validation it
reads the instance's own OpenAPI document and takes the printers, archives and
queue endpoints from that, whatever they are called on your build.

Three layers decide the path actually used, weakest first:

1. **Defaults** — `/api/printers`, `/api/archives`, `/api/queue`, spec at
   `/openapi.json`. Only used when the other two say nothing.
2. **Discovered** — read from the instance's OpenAPI document and re-read on
   every re-validation, so an upgrade that moves an endpoint is picked up.
3. **Yours** — anything set under **Settings → Bambuddy → Advanced** wins and is
   never overwritten by discovery.

If the spec names nothing PrintFlow recognises, Advanced lists every listable
endpoint the instance publishes and you pick from that list rather than typing a
path blind. A 404 during validation opens it automatically, because a 404 means
the address is right and only the path is wrong.

Queue payload field names are stored alongside, and still edited by hand:

```json
{ "fields": { "archive_id": "archive_id", "plate_number": "plate",
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
cd backend && pytest              # 226 tests
```

The suite covers the state machine and decisioning maths as pure functions, the
full intake pipeline against a real PostgreSQL (bundle explosion, soft
reservations, plate planning, dispatch, reconcile, assembly gate, failure and
re-queue), the HTTP API including the setup wizard and label purchase, the
integration clients (credential encryption, PKCE, retry/backoff, rate limiting,
response parsing), the security bootstrap (secret-key generation and
persistence, certificate generation and validation, the security endpoints), and
the tunnel (command building, log parsing, and the supervisor driven against a
stand-in `cloudflared` to check it starts, restarts and stops cleanly).

`TEST_DATABASE_URL` overrides the test database.

### Layout

```
backend/app/
  config.py            data dir + secret key bootstrap
  server.py            HTTP + HTTPS listeners, hot certificate reload
  models.py            schema, state vocabularies
  services/
    tls.py             certificate generation, validation, storage
    tunnel.py          cloudflared config + supervised child process
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

No writes to Etsy. In QuickBooks, only made-items sheets — no invoices, bills,
journal entries or other accounting features. No multi-user roles.
No multi-level BOMs (a bundle may not contain a bundle — this is validated). No
automatic label purchase. No filament or spool tracking — Bambuddy owns that.
