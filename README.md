# PrintFlow

Order-orchestration platform for a 3D printing business. It does not replace
Etsy, QuickBooks Online, Bambuddy or ShipStation — it is the connective tissue
between them and the single pane of glass over the whole fulfilment flow.

| Platform           | Role                                        | Direction                       |
| ------------------ | ------------------------------------------- | ------------------------------- |
| Etsy               | Sales channel — source of orders            | Read only (poll receipts)       |
| QuickBooks Online  | Inventory system of record — quantity on hand | Read for orders; writes only a made-items sheet you post |
| Bambuddy           | Print farm manager — queue, archives, file manager | Read/write (queue, track) |
| ShipStation        | Shipping — labels, tracking pushback to Etsy | Read/write (match, buy labels) |

PrintFlow itself owns the order state machine, the flow board, the bundle
(BOM) definitions and the product → print-file mappings.

## The workflow

1. An order arrives from Etsy and is polled in.
2. Line items are matched to products by the **Etsy listing and variation ids**
   the receipt carries (see [How orders find their product](#how-orders-find-their-product)).
3. Bundles explode into their components using the BOM table, adjusted by
   whatever options the buyer picked on Etsy (see below).
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

## Checking products against Etsy

**Products → Check against Etsy** reads the shop's live listings and lines them
up against the product table, before any order depends on them. Intake already
reports a line with no product, but only once a real order has arrived and
stalled on it; this is the same comparison made while there is still time.

It is also how a shop gets set up. **Create all as printed** (or *as stocked*)
builds a product from every listing that has none, links each one to its listing
as it goes, and clears any orders that were already waiting. Nothing has to be
typed, and no SKUs have to be invented.

Four answers, and all four are worth seeing:

* **matched** — the listing carries a SKU and a product has that code.
* **linked listing** — the listing is pointed at a product. This is the normal
  answer, and the one every imported listing gets.
* **no product** — an order for this will stall on arrival. Create the product
  from the row, printed or stocked, or link an existing one — without leaving
  the screen.

Products no live listing sells are listed separately. Usually those are bundle
components, which is expected — a finished good sitting there is worth a look.

The Products screen is split by what a product is — **Printed items**, **Stocked
items**, **Bundled items** — because the three are worked on at different times:
print files for one, stock levels for another, a bill of materials for the
third. Variants stay nested under the master they belong to.

Products can be removed from the same screen: tick the rows and **Delete
selected**. Anything that cannot go is kept and says why — an order that
references it, a bundle that uses it as a component, an option rule that brings
it in, or a made-items line that recorded making it. Those last two are
accounting history and are never rewritten, so mark the product inactive
instead.

Each row also carries the listing's own option values, so a
[BOM option rule](#etsy-options-that-change-the-bom) can be written from the
listing before a single order has arrived carrying that option.

**This needs a permission your Etsy connection may not have.** Reading listings
requires the `listings_r` scope, which older PrintFlow connections were never
granted. Order polling is unaffected and keeps working; only this screen needs
it. If yours predates it, the screen says so and asks you to reconnect Etsy from
**Settings → Etsy** — the authorisation page now requests listing access along
with orders.

## How orders find their product

Etsy does not require a seller to fill in a SKU, and plenty of shops never have.
So PrintFlow matches on what Etsy sends on **every** receipt instead: the
**listing id**, and the **product id** of the exact variation the buyer bought.
The order of preference is:

1. a link to that exact variation;
2. a link to the listing as a whole;
3. the product code, if the receipt happens to carry a matching SKU.

The Etsy identity comes first deliberately. A link is what somebody decided on
purpose, and a stale SKU left in a listing must not silently override it. Shops
that do keep SKUs in Etsy still work: with no link on the listing, step 3 matches
exactly as before.

Links are made in three places:

* **In bulk.** **Products → Check against Etsy → Create all**. This is the setup
  path, and the one to reach for first.
* **From an order.** Match the line as usual and tick *"Remember this
  listing"*. Every other line already waiting on the same listing matches at the
  same time, and the drawer says how many.
* **From a product.** **Products → (a product) → Etsy listings**, by pasting a
  listing id.

Prefer the whole listing over a single variation. Etsy issues a **new product id
for a variation whenever the seller edits the listing's options**, so a
variation-scoped link quietly stops matching after the next edit. Differences
between variations belong in
[option rules](#etsy-options-that-change-the-bom), which match on the option
values themselves and survive an edit.

### Product codes

Every product carries a short code, used only for display — on a card, in a
QuickBooks description, in an error message. It is **optional**: leave it blank
and PrintFlow generates one, either from the Etsy listing (`ETSY-1895497697`,
which pastes straight into an Etsy URL) or from the product's name. Nothing
matches on it unless a receipt happens to carry that exact SKU and the listing
is not linked to anything.

## Variations

A listing sells one product in several combinations — *Bin Fan: Yes* and *Bin
Fan: No* — and those are not interchangeable: one is a different plate on the
printer, or a different item drawn down in QuickBooks. **Products → (a product)
→ Variations → Pull from Etsy** reads the listing and makes a row per
combination. Nobody types option names: they have to match Etsy's exactly, and a
typo there is silent — it prints the wrong plate and nobody finds out until the
parcel is open.

Orders attach to a variation automatically, most specific first:

1. Etsy's own variation id, when it is current;
2. the option values, compared ignoring case and spacing.

Both are kept because Etsy **reissues a variation's id whenever the seller edits
the listing's options**. Matching on the id alone would go quiet after an edit
and take the wrong plate with it; the values survive.

What a matched variation *is* comes in two weights.

**Its own product** — *Give it its own components*. The variant becomes a real
product nested under the master, with its own BOM, print file and QuickBooks
item. This is the right answer when the combinations are genuinely different
builds, which is most of the time: "with fan" and "without fan" are two builds,
not one build with a substitution. The master stays what Etsy sells and what an
order matches first; the variant is what actually gets made, and an order
resolves through to it and explodes its components.

**A shortcut** — for when only the plate or the stock bucket differs and a whole
product would be ceremony, the row can override **the print file** or **the
QuickBooks item** directly. Ignored once the variation has its own product; that
product carries everything.

Blank on both counts is still worth having: the order says which one was bought.

Variants do not nest further. One level is enough to describe a listing, and
more would mean every reader has to walk a tree.

Setting variations up **after** the first order arrived is the normal way round,
so pulling them re-matches the open orders on that product, and rewrites any
print job that has not reached Bambuddy yet. A job already queued or printing is
left alone — it is a fact on a machine, and deleting the row would not unprint
it.

Combinations Etsy stops selling are marked *no longer sold* rather than deleted,
because old orders still point at them.

## The board, and who moves the cards

**Nothing moves a card except you.** Not intake, not a finished plate, not
buying a label. Drag a card to any column, Cancelled included; the drawer also
has a **Move** button, which is the way to do it on a phone, since dragging
needs a mouse.

This is a deliberate change from how it used to work. The rules can see that
four plates came off the printers; they cannot see that the parcel is still on
the bench, that the buyer rang up, or that this one is waiting on something from
elsewhere. A card that moves itself out from under whoever is working the board
is worse than one that waits to be moved. **In particular: an order no longer
advances to Ready to Ship when its prints finish, and buying a label no longer
marks it Shipped.**

What still happens on its own is the *work*: lines are matched, stock is
checked, plates are planned, queued and tracked, and every line's own state
follows from that. That is what the badges on a card tell you. The board itself
says nothing about where a card *should* be — when the rules disagree with where
one sits, the drawer offers a one-click move and nothing else mentions it.

Moving a card is not only a label; the status decides what the lines are:

* **Cancelled** cancels the lines, which releases the stock they were holding
  and keeps any plates that have not reached Bambuddy off the printers.
* **Shipped** carries the lines to shipped.

None of that is written down separately, so moving the card back recomputes it
all — including any line that was cancelled by hand, which comes back with the
order. An order that is not in Cancelled must not read as cancelled anywhere,
and that includes the rules: they never suggest Cancelled, because cancelling is
decided rather than observed.

Every move is written to the audit log with the note you gave it.

## Finding an order, and changing a match

The board draws the five live columns, so a cancelled order is not on it and a
shipped one scrolls away. **Orders** lists every order whatever its status, with
a filter per status and a search over order number and buyer, and opens the same
drawer.

Two ways back from a wrong match:

* **Change product** on a line hands it back to the picker. Anything queued for
  it that has not reached Bambuddy is dropped; anything already on a printer is
  left alone and said so, because forgetting the row would not unprint it.
* **Reset matching** on the order unmatches every line and runs intake again
  from scratch. This is the one to use after the catalogue changed underneath an
  order — a listing linked, variations added, a BOM corrected. Plain **Re-run
  intake** keeps whatever each line already matched; reset throws it away first.

## Print files, and which machines can make them

A printed product needs two answers: which file, and what can print it.

**The file** comes off Bambuddy. **Browse Bambuddy files** opens one picker with
three places to look, chosen at the top:

* **Bambuddy library (archives)** — every archived 3MF the instance has, not the
  first page of them, as a flat searchable list.
* **File manager (all printers)** — Bambuddy's own file manager, with its folder
  structure intact: folders open, breadcrumbs walk back up, and searching cuts
  across the whole tree and shows full paths. A shop that has sorted its files
  into folders has already said what is what, and flattening that into a list of
  filenames would throw the work away.
* **One printer** — that machine's own storage. See below; this is the case
  where picking the file also picks the printer.

Only what a printer can take is offered — 3MF and GCODE — and the count at the
bottom says how many other files were in the folder, so a picker with nothing in
it is never mistaken for an empty folder. If the file manager is bigger than
PrintFlow walks in one pass, or a folder cannot be read, it says so rather than
showing a short list and letting you conclude the file is missing.

The file manager endpoints are discovered from the instance's own OpenAPI
document, like the others, and can be corrected under **Settings → Bambuddy →
Advanced** — including the per-printer one, where `{printer_id}` is substituted.
An instance with one shared library can point both at the same path; the printer
then only decides where the plate goes.

**The machines** are chosen by *model*, and you can tick more than one. A shop
with four printers usually has more than one tool for a given job, and pinning a
file to a single machine means every plate of it queues behind that one machine
even when its twin is idle. The models come from the farm itself, with a count
of how many of each are online, so ticking one tells you straight away whether
it strands the job on a single printer.

* **Nothing ticked** means any printer: PrintFlow names no machine and Bambuddy
  places the plate itself. That is still the right answer for a shop with one
  kind of printer.
* **One or more ticked** and PrintFlow places the plate: a machine of a ticked
  model, preferring one that is online and idle over one that is merely busy,
  and a busy one over an offline one — a busy machine works through its queue,
  an unreachable one never starts. A run of plates spreads across the machines
  that can take them rather than stacking behind the first.
* **Nothing on the farm matches** and the plate stays queued in PrintFlow with
  a note saying what it was looking for. Sending it anyway would put it on a
  machine that cannot make it. The next poll tries again, so plugging in the
  right printer is enough to release it — no re-queueing by hand.

A variation can name its own models, which is how a taller version of the same
part ends up restricted to the bigger machine while the standard one still runs
anywhere. Left blank, it uses the product's.

### Picking a printer, then a file on it

Choosing a printer in the picker browses *that machine's* file manager, and the
models disappear — there is nothing left for them to decide. A file on one
printer's storage does not exist on any other, so the machine is settled by the
same act that settled the file, and dispatch sends the plate there without
consulting the model rules at all. Even a mapping whose models say something
else goes to the machine the file is on, because that is the only place it can
be printed.

Point the same mapping back at the library or the shared file manager and the
models come back with it.

Upgrading replaces the old single **preferred printer ID** with the models
above. There is no way to translate one into the other — which model a printer
id is belongs to Bambuddy, not to PrintFlow's database — so existing mappings
come through with nothing ticked, which is the behaviour they already had. A
mapping now needs an archive id *or* a file-manager path rather than an id
outright, since a file manager entry may have no id at all.

## Bills of materials, from QuickBooks

A bundle's BOM lists what it consumes. Components can be picked from the
products you already have, or — **From QuickBooks…** — straight out of
QuickBooks inventory, which is where the materials already live and the copy the
stock check actually reads.

The same is true of a [BOM option rule](#etsy-options-that-change-the-bom):
**Add from QuickBooks…** next to *Add rule*. A variation exists precisely
because it needs something the base build does not — the fan, the bigger magnet,
the second colour — so the thing it needs is by definition *not* on the BOM, and
a list of what is already there is the wrong list to be offered.

Picking an item finds the product that already points at it, or makes a stocked
one for it. Components stay products underneath because that is what the rest of
the pipeline works in: allocation reads a product's QuickBooks item, printing
reads its mapping, a made-items sheet rolls up its BOM. What it saves is the
step in the middle — retyping a material as a product and then linking it back
to the item you picked it from, which exists only to be got wrong.

## Etsy options that change the BOM

Buyers pick options on a listing — colour, size, "add a gift box" — and those
picks change what comes off the shelf. PrintFlow stores every option a
transaction carried, shows them on the order, and lets a bundle's BOM change in
response.

A rule on a bundle says: when this option has this value, either **swap** one
component for another, or **add** one.

```
Color = Red      swap PLA-GREY → PLA-RED
Gift box = Yes   add GIFT-BOX × 1
```

A swap keeps the quantity from the BOM line it replaces unless you override it.
Matching ignores case and surrounding spaces, because these strings are typed by
hand in Etsy's listing editor.

Rules live under **Products → (a bundle) → Etsy options**, and the option names
and values are offered **from what real orders have actually carried** rather
than typed from memory — a rule with a typo in it fires on nothing and says
nothing. Free-text personalisation is shown on the order but never offered as a
rule value; matching a rule against an arbitrary engraving message would fire on
coincidence.

Two things are deliberately loud rather than quiet:

* An order arrives with a value you have no rule for — say a new colour — and
  the line is flagged: *"No rule for Color = Teal. The base BOM was used."* It
  still flows through, because a missing rule must not strand an order, but the
  base BOM being used silently is exactly how the wrong filament ends up in the
  parcel.
* A rule that swaps out a component the BOM no longer has is skipped and
  reported, rather than adding its replacement on top and inflating the build.

Options are read from the receipt PrintFlow already stores, so orders taken
before this existed pick their options up on the next Etsy poll — nothing has to
be re-fetched. An order that gains options that way is re-resolved against its
BOM, since an option can change what it is made of. Only open receipts are
polled, so a shipped order keeps whatever it shipped with, which is correct.

To see it immediately on one order rather than waiting for the poll, open it and
press **Re-run intake**.

Two limits worth knowing: rules apply to **bundles**, since that is what has a
BOM — model an option-driven part as a bundle component to use them. And a
made-items sheet on the Manufacturing tab uses the base BOM, because there is no
Etsy order to read options from; pick the components you actually used there.

## Manufacturing (made-items sheets)

When you make items into stock rather than against an order, record it on the
**Manufacturing** tab. A sheet lists what you made and at what unit cost;
posting it writes **one QuickBooks Expense** carrying:

* a positive item line per thing made — quantity on hand goes **up**;
* a negative item line per component its BOM consumed — quantity on hand goes
  **down**.

A line names either a PrintFlow product or a **QuickBooks item picked straight
from the list**, so stock that is not a product here — packaging, supplies,
sub-assemblies — can still be counted in. A direct item line has no BOM and so
consumes nothing; its cost is prefilled from the item's own cost in QuickBooks.

Picking an item that a product already maps to files the line against that
product, BOM and all. Otherwise the same physical act would post two different
ways depending on which picker you happened to use. Service and non-inventory
items are greyed out and refused: QuickBooks tracks no quantity on them, so
such a line would book the expense and move no stock at all.

Costed at the BOM roll-up, the two sides cancel and the expense totals zero: the
sheet simply moves value out of components and into finished goods.

Two accounts are configured under **Settings → QuickBooks → Manufacturing
postings**, and they do different jobs:

* **Paid from** (required). QuickBooks calls this the Purchase's `AccountRef`
  and treats it as the account the expense came *out of*, so it only accepts a
  **Bank** account — or a **Credit Card** account when the payment type is
  CreditCard. Anything else is rejected with error 6430, "Invalid account type
  used". A sheet costed at its BOM nets to zero and never touches this account,
  so a clearing account suits it well.
* **Value added goes to** (optional). When you cost items above their components
  to absorb labour or machine time, that difference is credited here and the
  Purchase totals zero. Leave it blank and the difference comes out of the
  "paid from" account instead — which reads as money leaving a bank account
  that nothing actually left.

Neither has a default and posting stays blocked until the first is set: which
accounts are right depends on your chart of accounts, and guessing would file
real money somewhere you did not choose. PrintFlow checks the account's type
before posting, so a wrong choice is explained here rather than returned as a
bare error code by QuickBooks.

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
  no product stay here with a red badge so the fix path stays visible; the
  fix is "link product" from the order drawer, which re-runs intake for that
  line.
- **In Production** — at least one line dispatched to Bambuddy. A failed plate
  holds the line here and flags it red with a one-click Re-queue.
- **Assembly** — production finished but the order contains a bundle, which
  needs a manual check-off per bundle. Orders without bundles skip this column.
- **Ready to Ship** — every line ready. The Create Label button goes live.
- **Shipped** — label bought, tracking shown.

Other screens: **Products** (CRUD, QBO item picker, Bambuddy file picker,
printer models, BOM editor), **Print Queue** (every plate across all orders,
re-queue/cancel),
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
