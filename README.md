# PrintFlow

Order-orchestration platform for a 3D printing business. It does not replace
Etsy, Wix, QuickBooks Online, Bambuddy or ShipStation — it is the connective
tissue between them and the single pane of glass over the whole fulfilment flow.

| Platform           | Role                                        | Direction                       |
| ------------------ | ------------------------------------------- | ------------------------------- |
| Etsy               | Sales channel — source of orders            | Read only (poll receipts)       |
| Wix                | Sales channel — source of orders            | Read only (poll orders)         |
| QuickBooks Online  | Inventory system of record — quantity on hand | Read for orders; writes only a made-items sheet you post |
| Bambuddy           | Print farm manager — queue, archives, library | Read/write (queue, track) |
| ShipStation        | Shipping — labels, tracking pushback to Etsy | Read/write (match, buy labels) |

PrintFlow itself owns the order state machine, the flow board, the bundle
(BOM) definitions and the product → print-file mappings.

## The workflow

1. An order arrives from Etsy or Wix and is polled in.
2. Line items are matched to products — by SKU, or by the **listing and
   variation ids** the order carries (see
   [How orders find their product](#how-orders-find-their-product)).
3. Bundles explode into their components using the BOM table, adjusted by
   whatever options the buyer picked on Etsy (see below).
4. For each component, QuickBooks quantity on hand decides print-or-pull:
   stock covers the need → allocate; shortfall → print the shortfall only.
5. Print jobs go to the Bambuddy queue — one queue item per plate, with the
   plate count derived from `ceil(qty_to_print / units_per_plate)`.
6. Bambuddy job completion advances the cards on the kanban board.
7. At **Ready to Ship**, you click Create Label. ShipStation's own Etsy
   connection pushes the tracking number back to Etsy.

**PrintFlow never writes to a sales channel.** Neither Etsy nor Wix is ever
written to. It writes to QuickBooks in exactly three
places, all described under [Orders in the books](#orders-in-the-books) and the
Manufacturing tab: a made-items sheet when you press Post, a printed line taking
its units out of stock, and an invoice when you raise one. Importing and
deciding an order only ever *reads* quantity on hand. Labels are never bought
automatically — they cost money, so they are always an explicit click.

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

### The same, for Wix

Wix Stores has first-class SKUs, so nearly every Wix line matches on the product
code and never needs any of this. Where a line has no SKU — or the SKU is not
what PrintFlow calls the product — the same three steps apply against Wix's own
identifiers: the **catalogue item id**, and the **variant id** of the exact
combination bought. Matching one by hand and ticking *"Remember this item"*
records it, and the next order matches itself.

Links are made in three places, as on Etsy:

* **In bulk.** **Products → Check against Wix**. See below — this is the one to
  reach for.
* **From an order.** Match the line and tick *"Remember this item"*.
* **From a product.** **Products → (a product) → Wix items**. A Wix catalogue id
  is a GUID and nobody should type one, so the way in is *Search the Wix
  catalogue*, which opens already filtered by that product's own code. Pasting
  an id is the fallback, not the path.

### Check against Wix

The Etsy catalogue check exists to *create* products, because Etsy is usually
where a shop's catalogue came from. The Wix one exists to *recognise* them: a
shop running both channels almost always built its Wix catalogue by importing
from Etsy, so every Wix item already has a product here under another name, and
the only work is joining them up.

Three rules, tried in order, and the screen says which one fired for every row:

1. **the product code** — the same comparison intake uses. If Wix and PrintFlow
   agree on a SKU, that is the end of the question.
2. **the Etsy listing's title** — for the shops the link table exists for, who
   never filled in a SKU anywhere. The Wix item was imported from an Etsy
   listing and carries its title, and PrintFlow stored that title when the Etsy
   link was made. So the match is transitive: this Wix item is that Etsy
   listing, and that listing is this product. A title two listings share is
   dropped rather than guessed at.
3. **the product's own name** — last and weakest, because a product's name is
   ours to edit and drifts from what either channel calls it.

Everything it recognises arrives ticked, so the work is unticking what looks
wrong rather than ticking what looks right — but nothing is linked until you
press the button. A wrong link sends real orders to the wrong product and is
noticed when the wrong thing comes off a printer.

Items nothing matches are listed too, folded away. Those are the ones a Wix
order will stall on.

Wix Stores has two live API generations and a given site may have either;
PrintFlow tries the newer one, falls back to the older, and shows which
answered.

The two channels' links are kept apart. A line carries one channel's identity or
the other's, never both, and PrintFlow consults only the one it has. Wix's
variant ids are stable across catalogue edits, so unlike Etsy's there is no
reason to avoid a variant-scoped link.

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

### When Etsy has none to give

Pulling only works for a listing whose options Etsy models as inventory. A
listing that names its scales in the title, or offers them made to order, has no
combinations to read — and a product with no variations has nowhere to put a
print file override or a QuickBooks item. **Add one by hand** is there for that:
type the option name and value, optionally name the combination, and it behaves
like a pulled one from then on. Orders match it on the option values, which is
why the form insists you copy Etsy's wording exactly; where an order has already
arrived, copy it from that.

A hand-made variation is never retired by a later pull. The sync switches off
combinations Etsy no longer offers, and "Etsy no longer offers this" is not a
statement anybody can make about one Etsy never offered — switching it off would
take its QuickBooks item out of use, silently. If Etsy *does* later offer the
same combination, the row you typed is taken over rather than duplicated: it
gains Etsy's id and keeps everything you set on it.

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

**Nothing moves a card except you, and the carrier.** Not intake, not a
finished plate, not buying a label. Drag a card to any column, Cancelled
included; the drawer also has a **Move** button, which is the way to do it on a
phone, since dragging needs a mouse. The single exception is delivery: when the
carrier says the parcel arrived, the card moves itself to **Complete** — see
[Delivery](#delivery-and-the-complete-column). That is the one thing on this
board nobody in the shop can observe, and the one status nobody has to set.

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

### Delivery, and the Complete column

Shipped is not finished — the parcel is in a van. The column used to hold the
order posted this morning and the one that arrived last Tuesday with nothing to
tell them apart, and it only ever grew.

**The tracking number is a link.** It goes to that carrier's own tracking page:
USPS, UPS, FedEx, DHL, OnTrac, Canada Post, Royal Mail, Australia Post and a
few others, plus the resellers — a label bought through Stamps.com or Endicia
is a USPS parcel, and USPS is who can say where it is. A carrier PrintFlow does
not recognise gets a number you can copy and no link, because a link to the
wrong carrier's "not found" page looks like an answer.

**Parcels are checked on their own.** Every shipped order with a tracking
number is asked about periodically, and when the carrier says delivered the
card moves to **Complete** with the carrier's own sentence as its note —
"Left with an individual at 2:03pm" is the part that answers *delivered where?*.
The check backs off as a journey goes on: hours apart at first, half a day in
the middle, and after about a month of a parcel that never arrives it stops
asking and leaves the card in Shipped where somebody will see it. An attempted
delivery is not a delivery, and a carrier that says "not delivered" does not
move anything — the standardised status code decides, never the wording beside
it. One parcel the carrier will not discuss is logged and skipped rather than
stopping the rest.

**Two days later the board stops drawing it.** The clock runs from when the
card entered Complete, however it got there, so dragging one out and back gives
it two fresh days. Each Complete card says how long it has left rather than
just disappearing one morning.

**Nothing is ever deleted.** A card leaving the board is a card the board
stops drawing — the order is in the Orders tab with its lines, its money, its
label and its whole history, filterable to Complete. Deleting an order would
delete what it earned and what it cost.

#### Switching it on

Delivery detection needs a credential PrintFlow does not otherwise have.
ShipStation's original API — the key and secret that import orders and buy
labels — has no opinion about whether anything arrived; only their newer API
answers that, on a different host with a different key from the same account.
So there is a **Tracking API key** box under Settings → ShipStation, and it is
optional.

Without it nothing else changes: the tracking numbers are still links, Complete
is still a column, and the 48-hour rule still applies — cards simply reach
Complete by being dragged rather than on their own.

**The key is always stored, and the check only reports.** Pasting it asks
ShipStation which carriers the key can see, and the panel says what came back —
in ShipStation's own words, including the status code, if it came back badly.
It does not refuse to save.

That is deliberate, and it is a correction. The first version probed with a
made-up parcel and refused any key that probe disliked, which is the wrong way
round twice over. Asking about a parcel needs a carrier code and a tracking
number to be right *as well as* the key, so a refusal could equally mean "that
carrier is not on your account" — and it did: a perfectly good key was rejected
with a confident message about being the wrong key. A credential check should
ask the key about itself, and it should not overrule the person holding it. The
cost of storing a key that turns out not to work is that deliveries are not
detected, which is exactly what happens if it is not stored, except now the
reason is on the screen. **Check it now** re-asks at any time, for when it
worked in March and deliveries stopped in June.

### What the label costs, and what it cost

Buying a label is the only thing PrintFlow spends money on, so the price is
shown on both sides of the click.

**Before.** Pick a carrier and a weight and the label dialog asks ShipStation
what each of that carrier's services would charge, and prints the answer beside
every line of the Service dropdown — *UPS® Ground — $28.05*, *UPS Next Day Air®
— $523.53* — with the chosen one spelled out under the form and repeated on the
confirm button. Every service is priced in **one** request, not one per line:
the dropdown has a dozen entries and the operator is choosing between them. The
quote re-asks when the carrier, weight, units or package change, half a second
after typing stops.

It says **"about"**, and it means it. The carrier prices again at the moment the
label is bought, and a surcharge that depends on something ShipStation has not
been told yet lands on the real charge and not on this one. A number that is
nearly always right is worth a great deal when the question is *is this the $28
service or the $523 one*; presenting it as the price would be a promise nobody
here can make.

Quoting needs a **ship-from** postcode, which an order does not carry. It comes
from the warehouse the ShipStation order names, falling back to the default
warehouse and then to any of them. An instance with no warehouse origin at all
says so rather than guessing — and, like a carrier that refuses to quote, does
not block the purchase. Not knowing the price makes for a worse screen, not a
broken one.

**After.** The price is recorded on the order and shown in three places: on the
**card**, next to the tracking number; in the **drawer**, on the line that
already says when the label was made and by which service; and in the **label
dialog**, which now stays open after the purchase to say what it actually cost
rather than closing on success and hiding the number at the only moment somebody
is thinking about it.

Postage and insurance are added together, because both are charged and both
appear on the shipping bill — a card showing only the postage would be quietly
wrong on any insured order. A label ShipStation did not price shows no price:
"nothing was said" and "it was free" are different, and only one of them should
read as $0.00. Labels bought before this release have no cost to show, since
the number was never kept.

## Where it goes, and what it made

Clicking a card opens the order in three tabs — **Lines**, **Shipping**,
**Money**. It used to be one scroll holding all of it, and those are three
questions asked at three different moments by people doing three different
jobs: what has to be made, where it goes, what it earned. Splitting them costs
one click and stops the delivery address sitting somewhere below the print
queue. The order's own actions — Re-run intake, Reset matching, View raw Etsy
payload — stay put whichever tab is open, since they belong to the order rather
than to any one view of it.

Lines opens first, because that is the tab somebody working the board wants.

**Ship to** is the delivery address off the receipt, with a **Copy** button. The
address shown is Etsy's own `formatted_address` where there is one — already
laid out for the destination country, which is the version to put on a parcel,
because address order is not the same everywhere and reassembling the parts here
would get some countries wrong. The parts are kept in the database alongside it,
so "everything going to Illinois" remains a question a field can answer.

**Money** is the whole picture of one order, which no single system holds:

* **Revenue** — what the buyer paid, all in, with the split beneath it: items,
  shipping, tax, discount. Shipping the buyer paid for is not the same kind of
  money as the item price, and a shop that wants to know whether its postage is
  covered needs the two apart.
* **Etsy fees**, **Marketing fees** and **Processing fees**, each as a positive
  amount taken off the top.
* **Shipping label** — PrintFlow's own, from [what the label cost](#what-the-label-costs-and-what-it-cost).
* **Net** — revenue less all of it. This is the figure none of Etsy,
  QuickBooks or ShipStation can produce on its own.

**Fee breakdown from Etsy** sits under the totals: every charge grouped the way
the totals group it, each line copied from the shop's payment ledger in Etsy's
own wording, with the date the ledger was last read. The totals answer *how
much*; this answers *for what*, which is the question that actually gets asked —
a marketing fee nobody expected is a decision to revisit, and it cannot be
revisited from a single number. A figure that looks wrong can be taken back to
Etsy as their own sentence rather than as our arithmetic.

### Why the fees arrive late

The address and the money the buyer paid are in the receipt PrintFlow already
stores on every order, so they cost nothing and are there the instant an order
arrives — including on orders taken long before any of this was being read,
which fill in on the next poll.

The fees are not in the receipt and cannot be: at the moment a receipt exists,
Etsy has not charged anything yet. They land afterwards on the shop's **payment
ledger**, sometimes days later. So they are a sweep that runs again rather than
something intake could have done once, and an order with revenue and no fees is
normal rather than broken — the Money block says which of *"none yet"* and
*"never looked"* it is, and **Check Etsy for fees** asks now instead of waiting
for the next poll.

Attributing a ledger line to an order takes a step, because a fee points at
whatever caused it and that can be the receipt, the payment that settled it, or
a single transaction. All three ids are collected and a line matching any of
them belongs to that order. Etsy does not label a line "marketing" either — it
says *"Offsite Ads fee for order 1234"* — so the buckets are decided by reading
the ledger's own words, which is why the words are listed in the source rather
than buried in a pattern. Anything that is not recognisably a fee is left out
entirely: a deposit is not a cost of selling.

The ledger is one stream for the whole shop, so it is read **once** per sweep
and shared out across every order in the window, rather than asked about per
order. The window is 45 days; fees older than that settled long ago.

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

**A product can hold several files, and usually should.** A shop whose library
is sorted by machine has the same part sliced once per printer model, and those
files are alternatives rather than a sequence — the part gets made once, from
whichever of them fits the machine that is free. So a product lists its files,
each ticking the models *that* file was sliced for, and **Add another file**
adds the next one. Which file a plate uses is not decided when the plate is
planned; it is decided when the plate is sent. See
[Several files, one part](#several-files-one-part).

**The file** comes off Bambuddy. **Browse Bambuddy files** opens on the **file
manager**, showing Bambuddy's own folder structure with the folders closed — so
what you see first is the shape of the library, top level and item counts, on
one screen. A shop that has sorted its files into folders reaches for the folder
it wants, not for every file it owns.

Folders open individually, and **Expand all** / **Collapse all** does the lot.
The search box is still there — it cuts across every folder at once and shows
full paths, which is the one time folders get in the way — but it is an optional
shortcut, not the way in: a picker that makes you type before it shows you
anything is a picker for people who already know the answer.

Two other places to look, from the same control at the top:

* **One printer** — that machine's own storage. See below; this is the case
  where picking the file also picks the printer.
* **Bambuddy library** — every archived 3MF the instance has, not the first page
  of them, as a flat searchable list. This is the fallback for a build with no
  file manager, and PrintFlow switches to it on its own rather than opening on
  an error.

Only what a printer can take is offered — 3MF and GCODE — and the count at the
bottom says how many other files there were, so a picker with nothing in it is
never mistaken for an empty folder. If the file manager is bigger than PrintFlow
walks in one pass, or a folder cannot be read, it says so rather than showing a
short list and letting you conclude the file is missing.

**Show what Bambuddy sent** is at the bottom of the picker, always, and prints
the replies behind the tree verbatim — the folder list, one folder's children,
the file list, and one folder's files. Every instance is self-hosted and none of
them are quite the same shape, so a tree that is subtly wrong — a folder in the
wrong place, one branch missing its files — cannot be diagnosed from outside at
all. It can be shown, which turns a round of guessing into one screenshot.

**When it comes back empty.** "No files" has three causes that look identical
from outside: the folder is empty, the reply held rows in a shape PrintFlow
could not read, or the endpoint answers but is not the file manager.
It reads all the usual shapes — a bare list, any of the common envelopes, one
wrapper around the payload, and builds that keep folders in one array and models
in another — but it cannot know them all. So an empty file manager says which
endpoint it read and how many rows came back, alongside the replies themselves.
Folders that arrived with no files in them get the same treatment — that is a
different fault from nothing at all, and it says so.

An instance that ignores the folder parameter and answers every request with its
top level says so too, rather than drawing the same folder nested inside itself
as deep as the walk is allowed to go.

### Where the structure comes from

Bambuddy's library is two collections, not a browsable path: `GET
/api/v1/library/folders` returns every folder with the id of its parent, and
`GET /api/v1/library/files` returns every file with the id of its folder. The
shape is already in the data, so PrintFlow reads both collections — following
their paging — and assembles the tree by id. Two calls, not one per folder, and
it brings back things a walk cannot reach: an empty folder, and a folder whose
parent has gone missing, which keeps its files instead of taking them with it.

The library's own file id is what is stored, so it is the same id Bambuddy's
other endpoints take. Paths are built from folder names because a path is what
a person reads; names are not unique, so two files that would land on the same
path both survive, disambiguated by id rather than one silently overwriting the
other.

Both collections are asked plainly first — no paging parameters at all — and
paged only on evidence, a count in the reply larger than what came back. A pile
of well-meant `limit`/`offset`/`page` parameters is a good way to be answered
with an empty list by an endpoint that validates its query, and an empty list
from a collection that has rows in it is indistinguishable from an empty
library.

Two things the collections may not hand over on the first ask, both of which
look like an empty library rather than a missing question:

* **A subtree carried inline.** Bambuddy answers "list folders" with the top
  level, each row hanging its whole subtree off it under `children` — so the
  structure is in the first reply already, one level down from the top of it.
  That is read first, because it costs nothing.
* **A folder list that really is one level.** Where the reply carries no
  nesting, PrintFlow works out how the instance answers "what is inside this
  folder?" and then asks, folder by folder. Two shapes, tried on the first
  folder and then used for the rest: a list endpoint that filters on a parent
  (`?parent_id=`, `?parent=`, `?folder_id=`), or — where it filters on none of
  them — `GET …/library/folders/{id}`, which is how a file manager drills in
  and which returns the folder together with what it holds. An instance that
  does neither is left alone rather than walked pointlessly.
* **A file list that wants a folder named.** If it comes back empty while
  folders did not, PrintFlow asks again per folder — `?folder_id=…`. Only the
  folders that said they hold something: the rows carry a `file_count`, and on
  a library organised by printer model most of the folders are containers with
  nothing directly in them. A file filed in no folder at all is the one thing
  that cannot be recovered this way, since there is no folder to ask about.

Both are recorded in the picker's endpoint line, so a slower read is never
mistaken for the fast one.

A row may say where it sits in either of two ways, and both are read: an id
pointing at its parent, or a path spelling the whole ancestry out. The path wins
where there is one — it needs no chain to resolve and cannot be broken by a
parent that did not come back in the same reply. Folders that a path implies but
nothing listed are made real, because the picker draws a folder's children and
nothing is a child of a folder that does not exist: a file under an unlisted
folder would be invisible rather than merely misplaced.

A build that instead lists one folder at a time is still supported and is used
when the library collections are not there. All of these are discovered from the
instance's own OpenAPI document, like the others, and can be corrected under
**Settings → Bambuddy → Advanced** — including the per-printer one, where
`{printer_id}` is substituted.

**You do not have to re-save Settings after upgrading.** Bambuddy serves several
hundred endpoints and moves them between releases, so a role added in a *later
PrintFlow* release was never discovered for a connection made before it existed
and would otherwise fall back to a default that is only a guess — a 404 on a
path nobody chose. So a 404 on **any** Bambuddy endpoint re-reads the instance's
document, adopts what it now says, and remembers it. An instance whose library
is at `/api/v1/library` has almost certainly moved its printers and its queue as
well, and none of that should be an operator's problem to notice.

That covers **sending plates**, not only browsing for them. Reading the farm and
posting to the queue heal on a 404 exactly as the file picker does, because a
build where you can pick a file and not print it is the worse half of the same
fault. A 404 created nothing, so the retry cannot queue a plate twice.

Not every build has one. If the instance has no *per-printer* file endpoint,
picking a printer reads the shared library instead and says so — the machine is
still a real answer to where the plate goes, even when it is not where the file
is kept. If it has no file manager at all, the picker falls back to the archive
library by itself and says so, rather than greeting you with an error you did
not ask for — name the right endpoint under Advanced and the folders come back.
Choose the file manager *deliberately* on such an instance and you get the
error, with the endpoints it really does serve listed in it.

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

### Several files, one part

Ticking models on a *single* file assumes one file will feed every machine that
can take it, and on a farm of mixed printers that is often false: a plate sliced
for a P1S is not the plate an H2D should run. The honest shape is one file per
machine type, which is exactly how such a library is already organised — a
folder per model, the same part inside each.

So a product's files are a list. **Add another file** appends one, each with its
own Bambuddy file, plate number, units per plate, print options and ticked
models. Together they answer all three questions at once: what the product can
be printed from, every machine the product can be made on — the union of what
its files tick — and which file belongs to which of those machines.

**The choice is made at dispatch, not at planning.** When an order arrives,
every file is written onto the plate as a candidate. The plate then waits, and
when it is actually sent PrintFlow picks among the candidates by what is free
*then*: a file naming a model that has a machine beats one that does not, a
file naming machines beats a file that names none, and among equals the plate
goes to the least loaded of them, so a run of plates spreads across the farm
instead of stacking behind whichever file happens to be first in the list.
Deciding earlier would mean deciding against a farm that has since changed.

A plate that has not gone out yet still names a file — the first candidate — so
the [Printers](#printers) screen has something to show rather than a blank row. Adding, editing
or removing a file re-plans any plate that has not reached Bambuddy, so a fourth
machine getting its own slicing is picked up by the orders already waiting.
Plates already on a printer are left alone.

Two consequences worth knowing:

* **Plate maths uses the least any file promises.** If the H2D slicing fits four
  and the P1S slicing fits two, four units is planned as two plates, because the
  machine is not chosen yet and planning for four would ship the order short.
* **No machine for any of them** and the plate stays queued, its note listing
  every model all the files were looking for — not just the first one's.

A file that names neither an archive nor a path is refused rather than saved
empty, since it is not a way of making anything.

### Picking a printer, then a file on it

Choosing a printer in the picker browses *that machine's* file manager, and the
models disappear — there is nothing left for them to decide. A file on one
printer's storage does not exist on any other, so the machine is settled by the
same act that settled the file, and dispatch sends the plate there without
consulting the model rules at all. Even a file whose models say something else
goes to the machine the file is on, because that is the only place it can be
printed.

This is per file, not per product. A product can perfectly well hold one file
pinned to the machine that keeps it and two more from the shared library, and
the plate goes wherever the file it ends up using lives.

Point the same file back at the library or the shared file manager and the
models come back with it.

Upgrading replaces the old single **preferred printer ID** with the models
above. There is no way to translate one into the other — which model a printer
id is belongs to Bambuddy, not to PrintFlow's database — so existing files come
through with nothing ticked, which is the behaviour they already had. A file now
needs an archive id *or* a file-manager path rather than an id outright, since a
file manager entry may have no id at all. A product that had one print mapping
comes through as a product with one file in its list, unchanged.

## Printers

**Printers** is the farm: a card per machine, live from Bambuddy, with the
plates PrintFlow sent to that machine underneath it. It replaces the old flat
Print Queue, which answered "what is outstanding" — a question the board already
answers per order. Standing in the shop the question is nearly always the other
one, *which machine should I be looking at*, and a list sorted by order cannot
answer it however it is filtered.

Above the cards, **the farm in one line**: how many machines there are, how many
are printing, idle or offline, how many plates are outstanding and how many
units those come to — and, where anything is running, when the farm comes clear.
That last one is the *longest* job remaining, not the shortest: the farm is free
when the last machine finishes, not the first. Any state PrintFlow has no word
for is counted under its own name rather than swept into "unknown", because a
machine that is calibrating or in error is exactly the one worth walking to.

Each card carries what its build will say: online or not, what it is doing, how
far through with how long left, the layer it is on, the file on it, nozzle, bed
and chamber temperatures, and what is loaded in each AMS tray with how much is
left on it. Every reading is optional and a missing one is shown as missing
rather than as a zero — "no progress reported" and "0% done" are different
things to act on. A machine reporting a fault says so in red; a Bambu machine
reports "no fault" as the number 0, which is read as no fault rather than
printed as one.

### Telling a machine what to do

Each card carries a small toolbar, and the machine's own **Now** tab has the
same controls spelled out: **Pause**, **Resume**, **Stop**, and whatever else
your Bambuddy turns out to offer.

On a card it is three buttons — a play/pause glyph, a stop glyph, and a **⋯** —
because the Printers screen is a glance across the whole farm, and ten machines
showing five labelled buttons each is fifty labels competing with the readings
you came to look at. Pause and Resume share the first slot, since they are never
both available and two buttons of which one is always dead is a button's worth
of card spent saying nothing. Everything past Pause, Resume and Stop lives
behind the ⋯. On the machine's own page there is one machine and room to read,
so it is all written out.

**Which buttons exist is your instance's answer, not PrintFlow's.** They are
read off its own OpenAPI document, the same way every other endpoint is, and
there is no default for any of them. A build with no pause endpoint gets no
Pause button rather than one that posts into thin air, because a button that
looks like it worked is worse than no button — the operator walks away believing
the machine stopped. It also means the list is not limited to three: a build that
offers to home the bed, flick the chamber light or run a calibration gets an
entry for each under the ⋯, named as the build names it. Spelling is normalised
on the way in, so a build that says `cancel` or `abort` still produces one
**Stop**.

**Which buttons are pressable is what the machine is doing.** Pause only while
something is printing, Resume only while something is paused, Stop while either;
anything that moves the head or the filament only while the machine is free.
What does not apply is greyed with the reason on it — "Nothing is paused" —
rather than disappearing, and the number of slots never changes: the shared
Pause/Resume slot falls back to a greyed **Pause** when neither applies. A
toolbar that grows and shrinks between the glance and the click is one where
**Stop** moves under the cursor.

Two exceptions are worth knowing:

* A machine that is **offline** takes no orders at all.
* A build that reports **no live state** gets everything enabled. That is not
  the same as a machine doing nothing — it means the build did not say — and
  greying every button out over a reading PrintFlow never got would take the
  controls away from exactly the shops that most need them.

The page refreshes every fifteen seconds, so the card you are looking at can be
up to fifteen seconds stale — which matters when the print it offers to pause
has just finished. So the state is checked again, server-side, in the moment
before the control is sent: usually from the farm listing that was going to be
read anyway, and from the machine itself only on builds whose listing carries no
readings. A Pause aimed at a finished print is refused rather than landing on
whatever started next. Everything sent is written to the audit log.

Nothing is retried. Every other Bambuddy call is a read and repeating one costs
nothing; these change what a machine is physically doing, and a Stop quietly
sent twice is not the same as one sent once.

If your build has a control PrintFlow did not recognise, its path can be set by
hand under **Settings → Bambuddy → Advanced**, alongside every other endpoint.

### The queue on each machine

Each card says how many jobs are lined up on that machine and what is next, and
the machine's own page lists the whole line in the order it will be printed.

**It is Bambuddy's queue, not PrintFlow's list.** That distinction is the whole
point: the line holds plates dispatched from orders *and* whatever anybody sent
from Bambuddy's own screen or from Print a file. A queue showing only the half
PrintFlow put there would describe a machine as free while it works through six
of somebody's test pieces. So every job is drawn, the ones PrintFlow recognises
are labelled with their order number, and the rest say *not an order* — which is
information, not a gap.

The whole farm's queue is read in one request rather than one per machine, and
a queue that cannot be read is a message rather than an error: a machine's
temperature is not less true because its queue endpoint moved. Jobs Bambuddy has
not assigned to a machine yet — it sends those to whichever comes free — get
their own group under the cards, because a job nobody can see is a job nobody
cancels.

**Cancel** takes a job out of the machine's queue. Where that job came from an
order, its plate is cancelled with it: a plate still marked queued for a job
that is in no queue is a disagreement nobody notices until dispatch tries again.
Cancelling something that never came from an order is fine and says so.

### One machine's own page

Click a machine's name and it opens on its own, in four tabs, because the
questions are different:

* **Now** — the controls, the camera at full size refreshing every second, the
  progress bar, temperatures, fan, speed, light and door, and the PrintFlow
  plates on it.
* **Queue** — the whole line on this machine, in order, with what each job is
  for and a way to take it out.
* **Machine** — what it *is* rather than what it is doing: model, serial,
  firmware, address, nozzle size and type, Wi-Fi signal, prints completed, and
  every filament tray (see below).
* **Everything** — literally every field this Bambuddy sent for this machine,
  flattened to `parent.child` and unedited. PrintFlow cannot know what a given
  build reports, so "all available metrics" is only honest if it means all of
  them, including the ones nothing here has a name for. Underneath it, the raw
  replies, for a machine whose card came back blank.

### What is loaded

Filament shows as a chip per tray on the card — a dot in the spool's own
colour, the material, and how much is left — and in full on the machine's own
page, with the slot number and which PLA it is: "PLA Matte" and "PLA Basic"
print differently, and the operator is choosing between reels rather than
between materials.

The colour is drawn rather than spelled out, because a cell reading `F55C1AFF`
is a code for a colour rather than the colour. Bambu sends eight hex digits —
the colour then its opacity, and the opacity is the *last* pair, so reading the
wrong end turns every opaque spool into a shade of nothing. Six digits, three,
a comma-separated triple and the names of the colours filament is actually sold
in all work too. Anything else keeps the word the instance sent and goes
without a swatch: "Galaxy Purple" is what is written on the reel somebody is
hunting for, and a square in roughly the wrong colour is worse than no square.
Where there is a word it is kept beside the swatch as well, since a colour on
its own is no use to anyone reading the screen rather than looking at it.

A tray at a tenth of a reel or less is tinted amber and labelled *nearly
empty*, because that is the level at which starting a long print stops being a
safe thing to do. A machine that reports no level at all is left plain: silent
is not the same as low, and colouring it would invent a reading. A slot that
reports nothing at all reads as *empty* rather than as an unknown filament — it
may simply have nothing in it, and calling that unknown sends somebody to check
a spool that is not there.

Where that comes from depends on the build, and PrintFlow handles both without
being told which it has. Most instances put the AMS on the printer row itself;
some keep it on an endpoint of its own, and for those the endpoint is found in
the instance's OpenAPI document (`/printers/{id}/filament`, `/ams`, `/spools`,
`/trays`…) and asked once per machine that reported nothing.

Either way the spools are **searched for rather than read from a fixed place**,
because the shapes differ by more than a key name. Bambu's own is
`ams.ams[].tray[]` — a list of AMS *units*, each holding its trays — which no
amount of looking one level down will find; others send a bare list, a wrapper
with `trays` in it, units inside modules, or a single spool. The one thing they
all agree on is what a spool looks like once you are standing on it, so that is
what the search looks for: a dictionary carrying a filament type, a colour or a
remaining figure. An AMS *unit* carries an id, a humidity and a temperature and
is therefore not a spool — reading it as one would draw four trays of unknown
filament that are not in the machine.

The external spool that feeds past the AMS is included as well, labelled *Ext*
rather than by its number: Bambu calls it tray 254, which is an internal id and
not anything written on the front of the machine. An empty slot is left out
entirely. Where the machine's own tray numbers are unique they are kept, so the
screen matches the printer; where they are not — two AMS units both number
their trays from zero — they are replaced by a straight count, because two rows
labelled the same is worse.

**A machine that reports no spools says so, and shows its reply.** Empty is two
different things: a machine with nothing loaded, and an AMS in a shape PrintFlow
has not been pointed at. Only the instance can tell them apart, so the Filament
section prints what the machine actually sent rather than leaving a blank —
the same move as the camera panel, and for the same reason.

That second call has to be free on the builds that do not need it, so the
answer is stored beside the connection the same way the camera's is: a build
with no such endpoint is asked once, ever, and never pays a 404 per machine per
refresh afterwards. A machine that will not answer keeps its card and its
readings — what is loaded is the least important thing on a card, and the card
is worth more than the trays. If the endpoint is somewhere the matching rules
do not recognise, it can be named under Settings → Bambuddy → Advanced as *One
printer's filament*, with `{printer_id}` where the machine goes.

### Printing something nobody ordered

**Print a file**, on that same page, sends anything in Bambuddy's library
straight to the machine you are looking at — the reprint, the test piece, the
jig. It uses the same file picker as a product's print files, so the whole
library and every printer's own files are there, and it asks for the plate
number and how many copies before it does anything: this spends filament on a
machine that may have something on it already.

**Pressing Print twice does not print twice.** The dangerous case is not a
double click, it is an answer that never arrives: a proxy in front of PrintFlow
times out, or a tunnel drops, and the operator sees an error for a plate that
is already on the machine. Nothing in the browser can tell that apart from a
request that never landed, and guessing wrong costs a plate of filament. So the
dialog carries one id for as long as it is open and sends it again on a retry;
a request that has already been done reports what it did — *"had already gone
to H2D-01 — the error was the reply going missing, not the print"* — rather
than doing it again. A genuinely new print gets a new id, so two deliberate
prints of the same file are still two prints.

It deliberately does not go through dispatch. Dispatch chooses a machine by
printer model and what is free; the point of asking here is that the operator
has already chosen by clicking. Nothing about it touches an order — it will not
count towards one, and the plate is cleared by hand like any other — and it is
written to the audit log, because it puts filament through a printer. If the
machine refuses partway through a run of copies, the error says how many had
already been queued, which is the difference between "try again" and "try again
and cancel three".

Above the readings, the **camera**, where the machine has one. Click it and the
picture fills a panel with the machine's state beside it, refreshing every
second. A machine with no camera takes the space back and says so; it is not an
error worth a banner.

Underneath, the plates. This is the half Bambuddy cannot answer: it knows a
machine is at 37%, not that the plate belongs to order #1042. **Cancel** and
**Re-queue** are on each plate, where the Print Queue had them.

Two groups sit below the farm, and both exist because a plate that is not on a
machine is a plate nobody would otherwise see:

* **Not on a machine** — waiting to be sent, or sent to a printer the farm no
  longer lists. A plate whose files all name models with none on the farm stays
  here until one appears, and says which models it is waiting for. **Send N
  waiting plates** pushes them all now rather than at the next poll.
* **Finished plates**, folded away. History, but the kind somebody asks about.

**When Bambuddy is down the plates are still there.** The farm read is allowed
to fail on its own: the machines go, a message explains why, and the queue and
its buttons stay — that is precisely when an operator needs them. And a plate on
a machine that has since been unplugged moves to *Not on a machine* rather than
disappearing with the card.

### Where the readings come from

The farm listing is asked first and plainly, because on most builds it already
carries the live values and one call is the whole answer. Only where it comes
back a bare inventory — names and models, nothing about what any of them is
doing — is each machine asked about individually, at one call per printer.

That per-machine endpoint is discovered from the instance's own document like
the others, preferring `…/printers/{id}/status` over `…/printers/{id}`: the
first is what the machine is doing this second, the second may be no more than
the inventory row again. It is corrected under **Settings → Bambuddy →
Advanced**, where it appears as *One printer*. A 404 there heals exactly as the
rest do, and once for the whole farm rather than once per machine — re-reading
the document ten times to learn one path is ten fetches for one fact.

The listing stays the spine either way: the detail only fills in what the
listing left blank, so a thin reply cannot blank out a name that did arrive. A
machine that will not answer keeps its row and the farm says which one refused —
a printer PrintFlow cannot reach is still a printer, and a farm screen that
quietly drops one is worse than useless.

### The cameras

**Proxied, not linked.** Neither thing a browser would need to fetch a camera
directly is true of it: the API key lives on the server, and Bambuddy is on the
shop LAN while the person looking at PrintFlow may be on a phone through the
tunnel. So the picture comes through PrintFlow, under the same login as the rest
of the screen.

**Pictures, not a stream — deliberately.** Passing a live multipart stream
through would hold one socket per card open for as long as the tab is: ten of
them, through a tunnel, on a screen people leave up all day. And it would only
work on the builds whose camera is multipart to begin with. Asking for a *frame*
works on both kinds — where the camera is a stream, one frame is read out of it
and the rest dropped — so the page decides how often it wants a new picture
rather than the camera deciding for it. The cards take one on each farm read;
the enlarged view, where somebody is actually watching, takes one a second, each
fetched out of sight and swapped in only once it has arrived so the picture
never blinks through empty. A camera that stops answering slows the asking
rather than stopping it, since a camera comes back when its machine wakes up.

**The endpoint** is discovered like the others. A camera can have a whole family
of endpoints — one real instance serves ten under `…/camera/`, of which
`status`, `stop`, `test`, `check-plate` and `plate-detection` are not pictures
at all — so a path only counts when *every* segment after the machine is about
the camera, which is why `camera-settings` is not one. Among the ones that are,
the still (`snapshot`, `still`, `image`, `photo`) is preferred over the stream:
a frame is all PrintFlow ever wants, and asking for one beats opening a stream
to take its first frame and hang up. Both shapes are read, because builds
disagree
about which noun owns the other: `/printers/{id}/camera` hangs the camera off
the machine and `/camera/{id}` hangs the machine off the camera, and they name
the same thing. Separators are not different words, so `camera_stream`,
`camera-feed` and `camera/stream` all read alike — but a segment has to be
*entirely* camera-ish to count, which is why `camera-settings` is not one. It is
editable under **Advanced** as *One printer's camera*.

**A camera may not accept the API key.** Some builds gate cameras behind a
short-lived token of their own and refuse without one — and, usefully, say where
to get one in the refusal itself. That sentence is the whole protocol: it is not
an error to report, it is an instruction to follow. PrintFlow mints a token,
retries, and holds it for a couple of minutes so that ten cards do not mean ten
tokens.

What the refusal does *not* say is **how** the token should be presented — a
query parameter under one of several names, or a header under another. So the
ways are tried in turn, once, and the one that works is remembered next to the
connection. After that a picture is a single request, and an instance known to
want a token asks for one up front rather than spending a round trip being told
what it already knows. The minter is discovered like any other endpoint and is
editable under **Advanced** as *Camera token*.

Whether this build has cameras at all is asked **once** and remembered beside
the connection: the alternative is either ten broken pictures on every page
load, or re-reading the instance's document on every page load to avoid them.
Three things ask again — re-validating under Settings, pressing **Look again**
below, and a PrintFlow release that widens what counts as a camera. That last
one matters: a stored "this build has none" was an answer to the rules of the
day, and the shop upgrading precisely to get their cameras working must not be
the one shop the fix cannot reach.

### When there are no pictures

A card with nothing on it has four causes that look identical from outside: the
build has no camera, it has one under a name PrintFlow does not recognise, it
has one that is not in its OpenAPI document at all, or it has one that answers
with something that is not a picture — an HTML login page comes back `200` and
would leave the same blank card. Each has a different fix and only the instance
can tell them apart, so when no machine is showing a picture the page offers
**Why?**, and answers with what the instance said rather than a guess:

* the endpoint PrintFlow is calling, and whether that came from the instance,
  from Advanced, or from a default that is only a guess;
* every endpoint the document mentions that sounds remotely like a camera —
  looser than the matcher on purpose, because a near miss somebody recognises
  is worth more than a short list that is certainly all cameras;
* what came back when it actually called one, including the first bytes, which
  is how a JPEG (`ffd8…`) is told from a web page without guessing at the
  content type.

The fix for the middle two is the same: put the path under **Settings →
Bambuddy → Advanced** as *One printer's camera*, with `{printer_id}` where the
machine goes, and press **Look again**. A hand-typed path is never second-
guessed — if it is there, PrintFlow uses it and does not go looking.

**A camera somewhere other than Bambuddy is not fetched.** Some builds name the
camera as a URL on the printer row rather than serving an endpoint for it. Where
that URL points back at the Bambuddy this connection already talks to, it is
followed. Where it points elsewhere on the network — the printer's own address,
say — PrintFlow does not go there: the URL arrives from outside and would be
fetched by the server on behalf of whoever opened the page, which is not
something an endpoint should offer however narrowly it is guarded. The card
shows it as a link instead, which a browser on that network can still follow.

If the cards come back with no readings on them at all, the page says so and
offers **Show what Bambuddy sent** — the printer listing and one machine's
detail, verbatim, the same diagnostic the file picker carries and for the same
reason. No two self-hosted builds spell a temperature the same way, and a blank
card cannot be diagnosed from outside. It can be shown.

## Maintenance

**The book of what has been done to each machine, and when.** A list of the
printers you service; selecting one opens it, with everything about it and its
whole log in one place. Entries carry four things: **hours**, **date**,
**notes**, **status**.

A popup rather than a row that expands. The list is for *choosing* a machine —
a shelf of them read at a glance, worst first — and the book is for *working on*
one, so they are two things rather than one thing that grows under the cursor.
It also gives the log the full width, which starts to matter once a machine has
a year of entries. Editing a machine swaps that panel rather than opening a
second dialog on top of it.

**These records are PrintFlow's own.** Everything else it knows about a printer
comes from Bambuddy, is read live, is cached nowhere, and is gone the moment
that connection is re-pointed or the printer re-added under a new id. A
maintenance history cannot work that way. It is the one record about a machine
that has to outlive changing farm managers, re-imaging the box, and the machine
leaving the farm altogether — and plenty of shops service printers no farm
manager ever saw. So the rows here are ours, and nothing on this tab needs
Bambuddy, Etsy or QuickBooks to be connected at all.

Adopting a machine from the farm is offered when you add one — it fills the name
and model, and remembers which farm printer it is. That link is a convenience
and only a convenience: clear it, break it, or point PrintFlow at a different
Bambuddy, and every entry is still there.

**Hours are the machine's own counter**, not how long the job took. "Nozzle
changed at 1,240 hours" is what makes the next change predictable; how long it
took to change is not. Leaving it blank is a real answer — plenty of entries get
written up at the end of a shift rather than at the screen — and an invented
zero would put a false reading between two true ones. Anything that is not a
number is refused rather than stored as zero.

**Status is what the machine is once that entry is written**: Serviced, Running
fine, Service due, Needs attention, Out of service. The newest entry *is* the
machine's condition — rather than a separate field somebody has to remember to
change — so it can never drift from the history that explains it, and it is a
thing they were going to write down anyway.

Machines wanting a look sort to the top, with the ones nobody has logged yet
just under them; a list in name order buries exactly the machine somebody opened
the tab to find. **Retiring** a machine keeps its whole history and takes it out
of the way, which is what is wanted nearly every time; deleting one is offered
too, for the machine typed in by mistake, and takes its book with it.

Entries can be corrected and deleted — this is a shop's own notebook rather than
an accounting record, and a typo in an hour reading should be crossed out rather
than lived with. The crossing-out is kept in the audit log, which is also the
only place a deleted entry survives. Every entry is signed with whoever wrote
it: a maintenance log nobody signed is one nobody can ask about.

The whole book is in the backup, like everything else.

## Calculator

**What a printed part costs to make**, and what it would have to sell for. Five
costs kept apart on purpose — a total nobody can take apart is a total nobody
argues with, and the argument is the point: a part that looks expensive is
usually expensive for one reason, and the breakdown says which.

| Line | What it is |
| --- | --- |
| Filament | Grams off the spool at what the spool cost. |
| Electricity | How long the machine ran, at what it draws and what a unit costs. Small per part; not small per month. |
| Machine time | The printer wearing out — its own price over its life, plus nozzles, belts and the afternoon spent fixing it. Usually the largest line after filament, and the one shops forget. |
| Labour | Slicing, plate changes, supports off, sanding. Charged by the minute, because that is how it is spent. |
| Extras | Packaging, an insert, a magnet. |

Then two things that are not costs of a *successful* print but are costs of
printing: a **failure allowance**, applied to everything above it because a
failure wastes the machine hour as surely as the filament, and a **markup** that
turns the cost into a price. The price panel shows what is kept and the margin,
and says plainly that it is before Etsy's cut and before postage.

Rates are typed once — machine, power, labour, failure, markup are the same for
everything you print — and remembered. None of them has a default: a machine
rate nobody chose would look worked out and would not be, and the point of the
calculator is a figure you can defend. Spool prices can be saved by name so the
filament price is picked rather than looked up.

The arithmetic runs on the server in Decimal, beside every other figure here
that is money, and every line is rounded to cents *before* anything is added —
so the column on screen adds up to the total on screen. It writes nothing: not
to QuickBooks, not to an order, not even to the estimate. Sending a rate with a
part answers "what would this cost at a different rate" without changing what
the shop is set to.

The figure at the bottom is the other thing being spent: how long a machine is
tied up. An eight-hour part at a good margin can still be the wrong print.

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
reads its print files, a made-items sheet rolls up its BOM. What it saves is the
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

**Where a line's cost comes from** is decided by whether the product has a BOM,
and the line says which under the figure:

* **it has one** — the components are rolled up. That is what the thing costs to
  make, and it beats anything QuickBooks holds about the finished item.
* **it has none** — QuickBooks is asked what the item itself costs, the same
  figure a direct item line gets. A product made from materials that are
  expensed on purchase has nothing to roll up, and a zero on a line that reaches
  your accounts is a worse answer than a real cost you can overrule.

A BOM that *cannot* be priced — a component with no cost on it — still leaves the
line at zero rather than quietly borrowing the finished item's cost. Half a
roll-up is worse than none, and so is a plausible substitute for one.

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

Guardrails, because this writes to your books:

* Nothing posts without a person pressing Post. No background job posts.
* The sheet shows exactly what will happen — both totals and the difference —
  before you commit, and again in the confirmation.
* A posted sheet is immutable. Correcting one means Void (which deletes the
  QuickBooks transaction and reverses every quantity) plus a new sheet.
* Each posting carries an idempotency key, so a timeout followed by a retry
  cannot produce two transactions.
* A product with no QuickBooks item linked blocks the post rather than silently
  posting a sheet that moves nothing.

## Orders in the books

Four things happen to an order in QuickBooks: its units leave stock when they
are printed or built into a bundle, the sale is recorded when you invoice it,
and what the order cost — postage and the shop's cut — is expensed. None of it
cares which channel sold the order: an invoice's private note names the channel
so a reconciler months later knows where to go and look, and that is the whole
of the difference.

The first two are designed together because **both of them move stock**, and
left to themselves they would move the same units twice. What follows is how
they hand over instead. The expenses are simpler: they are money going out, and
nothing else in the system touches them.

### Stock out when a line is printed

A printed line's units come out of QuickBooks as **one Purchase carrying two
lines that cancel**:

* an item line for the product's own QuickBooks item with a **negative**
  quantity — quantity on hand falls, Inventory Asset is credited;
* an account line for the same amount against the **cost of goods sold** account
  from Settings — the value of those units lands where a sold unit's value
  belongs.

The document totals **zero**, so the payment account it names is never touched.
That is the point of the second line: a Purchase carrying only the negative one
would total below zero, which QuickBooks reads as money *arriving* in a bank
account, and nothing arrived.

QuickBooks relieves inventory at its own average cost whatever unit price it is
handed, so an item with no recorded cost still moves the right quantity — the
document simply carries no value. A guessed figure would be worse.

**When it happens.** Pressing **Mark printed** always books it: that is a person
asking. A print that finishes on its own books it too, while *Remove stock when
a line is printed* is on under Settings. Turning that off restores the older
rule that nothing but a person writes to your books — the button still works.

**Once, ever.** The guard is a stored timestamp on the line, not its state. Line
states here are *derived* and recomputed from scratch on every pass, so "this
line is printed" is true again a minute later and again after that; only the
column can stop the second booking. A variation with its own QuickBooks item
draws down that item rather than its parent's, and a bundle is booked through
its components rather than as itself.

**Undoing it.** Cancelling a line whose units were already booked deletes the
Purchase and puts the stock back on its own. For the other case — a line marked
printed that was not — there is a **Put stock back** button on the line, because
clearing the override cannot be trusted to mean "undo the books": it is just as
often a tidy-up after a print that really did finish. Nothing reverses itself on
a line's state changing, so re-queuing a plate never churns documents in your
books.

A removal that could not be made says so on the line, with a **Retry stock
removal** button beside it — a book entry that quietly did not happen is the
failure worth designing against.

### An invoice, when you raise one

**Invoice** appears on every order in the Orders list and on every board card,
and in the drawer's Money tab with rather more detail. It is a button rather
than something that happens on its own: an invoice is a document in somebody's
books, and which orders get one is a decision about the business.

It bills the buyer's **name and address from the order** — finding that customer
in QuickBooks by display name, or creating them. Lines come from the order's
top-level lines at the price Etsy recorded, plus postage the buyer paid when
Settings names an item for it. Bundle
components are not itemised: the buyer bought a bundle, not its bill of
materials. A line whose price cannot be found is left off rather than billed at
zero, because a zero on an invoice looks like a decision somebody made.

**Every line names its own QuickBooks item** — the product's, or the variation's
where it has one. That is what lets QuickBooks report sales and cost of goods
sold *by item*, which an invoice of identical "Etsy sales" lines cannot. What
was sold is in the description too, options included, for whoever reads the
invoice.

**Which means the invoice moves stock**, for every line billed on an inventory
item: QuickBooks relieves quantity on hand and books cost from the invoice
itself. Those units were already taken out when the line was printed, so
**raising the invoice takes that earlier Purchase back**. The removal at print
time is provisional — it keeps QuickBooks honest between printing and billing —
and the invoice is the real document. A line billed on a service item (a bundle,
or a product QuickBooks does not track) keeps its removal, because nothing else
is going to make it.

Net: one deduction per unit, whichever way an order goes. The hand-over is
audited, so a deleted Purchase is never a mystery.

**The discount Etsy took.** Etsy discounts the whole basket rather than any one
item, and a QuickBooks discount line says the same thing — so that is what it
becomes, last on the invoice, applying to the subtotal above it. An order Etsy
sold at $5 off is billed at $5 off; billing the full price and pocketing the
difference would overstate revenue by exactly the discount, on a document that
ends up on a tax return. QuickBooks refuses a discount line outright when
discounts are switched off in the company settings, so PrintFlow checks first
and stops with the name of the switch rather than passing on an Intuit error
code.

**Which number it gets.** There are two arrangements and QuickBooks picks
between them by a setting inside your company file, so PrintFlow asks rather
than assumes:

* **QuickBooks numbers it** — the default, and the one to want. It applies the
  next reference in its own sequence as the document saves, so PrintFlow and
  somebody typing an invoice by hand cannot land on the same number. Nothing is
  sent; what it assigned is read back onto the order.
* **You number your own** — *Custom transaction numbers* is on, and QuickBooks
  assigns nothing at all. An invoice sent without a reference simply has none,
  which is a gap nothing else would have caught. So PrintFlow reads the most
  recent invoice and carries the sequence on: `1042` → `1043`, `INV-1042` →
  `INV-1043`, `0099` → `0100`. Only the trailing digits move and the padding is
  kept, because a sequence that changes shape halfway through is one somebody
  has to explain. With no invoices to count from it sends nothing rather than
  inventing a reference.

Either way the Money tab says which it will be **before** you press the button,
since the two are indistinguishable until an invoice turns up unnumbered.

**Voiding.** An invoice can be voided from the Money tab, which leaves a
zero-total document in QuickBooks — rather than deleting it and leaving a gap in
the numbering that somebody has to explain — and frees the order to be invoiced
again.

### Stock out when a bundle is assembled

Printing is not the only way a part gets used up. A component the shop already
had is **pulled from stock** — a decision PrintFlow makes without writing
anything down — so until the bundle was built, those units sat in QuickBooks as
available long after they had been screwed into something.

Assembly is the moment a pile of parts stops being parts, so ticking **Assembly
Complete** books whatever printing did not: one Purchase per component, the same
zero-total shape as a printed line's, described as *consumed assembling order
1042* so it can be told apart in QuickBooks from a unit that was sold. A
component already booked out by printing is skipped — one deduction per unit is
the rule the whole of this section is arranged around — and the bundle itself is
never booked, because it is a container and its components carry the items.

Unticking it puts back **only what the assembly took**. A component booked out
when it was *printed* keeps its removal, because unticking a box is not a
statement about a print that really happened. That distinction is why each line
records *why* its units went, not just that they did.

Assembly is a shop-floor action, so it never fails over bookkeeping: a component
with no QuickBooks item is simply not booked, and the tick still lands.

### What the order cost: two expenses

An order earns money and an order costs money, and until now only the earning
reached QuickBooks. Two bills arrive against a sale, from two different people
at two different times, so they are two documents rather than one — each raised
and removed on its own from the Money tab, because an order is often ready to
expense one and not the other.

* **The shipping label** — what ShipStation charged, once it has priced one.
  Not the same number as the shipping the buyer paid: that one is *income* and
  is billed on the invoice. Confusing the two is how postage looks free.
* **Etsy's cut** — its commission, Offsite Ads and payment processing, as one
  expense with a line for each. Three lines rather than a single figure called
  "Etsy", because a shop deciding whether Offsite Ads pays for itself cannot
  tell from one number.

Both are plain Purchases: an account line per thing paid for, against the
expense account chosen in Settings, out of the account chosen there too. Nothing
here is an item line — postage bought and a marketplace's commission are costs,
and putting them on an item would move a quantity of something nobody has.

Unlike a stock removal, **these really do take money out of the account they
name**, because the carrier really was paid and Etsy really did take its cut.
That is why the paid-from account is worth a thought rather than a default; it
falls back to the Manufacturing paid-from account, which is right for most
shops.

Each is once-only — the id on the order is the proof — and each carries a stable
idempotency key, so a timeout followed by a retry cannot produce two. **Remove**
deletes the Purchase and frees the order to be expensed again; a Purchase has no
void in QuickBooks that keeps it visible, which is the same reason voiding a
made sheet deletes its document.

### Typing Etsy's fees in

Etsy posts its fees to the shop ledger *days* after a sale. Until they land an
order shows no cost at all, so a shop closing its month either waits for Etsy or
works the figures out on paper.

The Money tab has a third option: **Etsy fees → Enter them**, three boxes, typed
by hand. They behave exactly like swept ones — they show in the summary, they
count against Net, and they expense the same way.

What is typed is marked as typed, and **Check Etsy for fees then leaves that
order alone**. A sweep quietly replacing a number somebody put there — and may
already have expensed to QuickBooks — is how the books and the screen stop
agreeing. Clearing every box hands the order back to the sweep.

Etsy's ledger states fees as money *leaving*, so a figure pasted from there
arrives with a minus sign; PrintFlow stores the size of the bite and reads
`-2.55` as the same thing as `2.55`. Anything that is not a number is refused
rather than quietly stored as zero.

**On a Wix order, typing is the only way.** Etsy is the one channel PrintFlow
sweeps a fee ledger from, so a Wix order shows no sweep button and says as much
rather than promising fees that are never going to arrive. Everything after the
typing is identical — summary, Net, expense.

### Auditing what left stock

The Manufacturing tab lists **Stock out, from orders** underneath the made-items
sheets. A sheet and a stock removal are both Purchases in the same books — one
puts stock in, the other takes it out — and only the sheets had a screen, so
reconciling a month against QuickBooks meant opening every order in turn to find
the other half.

Each row gives the order, the product, how many units, whether it was printing
or assembly that consumed them, when, and the QuickBooks Purchase. Failures are
listed with the rest rather than hidden: a removal that did not happen is the
row an auditor most wants, and it appears nowhere else.

### What to configure

Under **Settings → QuickBooks → Orders in the books**:

* **Cost of goods sold account** (required for stock removals). The removal also
  reuses the "paid from" account under Manufacturing postings; since the document
  totals zero, the same clearing account serves both.
* **Fallback item for lines with no item of their own.** Invoice lines name the
  real item sold, so this is only for what has none — a bundle, or a product
  QuickBooks does not track. It must be a **Service or Non-Inventory** item: it
  stands in for goods QuickBooks holds no stock of, so it must not pretend to
  move any. Inventory items are refused here and not offered by the picker. A
  line with nothing to name blocks the invoice rather than being dropped, which
  would send an invoice for the wrong total.
* **Shipping.** Which item postage the buyer paid goes on, or **None** — no
  item, no line, for a shop that accounts for postage elsewhere.
* **Discount account** (optional). Where a discount Etsy took lands. Income
  accounts are offered, because a discount is revenue not earned rather than a
  cost incurred. Left unset — the sensible default — QuickBooks uses the
  company's own discount account.
* **Shipping label expense** (required to expense postage). Where what the
  carrier charged lands.
* **Etsy fee expense** (required to expense fees). Where Etsy's cut lands.
* **Expenses are paid from** (optional). Which account the money left. These
  two are real money going out, unlike a stock removal, so the account matters.
  Left unset it reuses the Manufacturing paid-from account.

Nothing has a default. Which account and which item are right depends on your
chart of accounts, and picking on your behalf would file real money somewhere
nobody chose. Each unset setting says, where it matters, what it is blocking.

### "This product has no QuickBooks item"

An invoice refused for this reason names the products it could not bill, by
**code as well as title** — an Etsy listing title is long and punctuated and is
not what the Products search matches on. There are two ways out, and which one
you want depends on how many products it is:

* **Link the product to an item**, on the Products tab. Every fulfillment can
  have one, bundles included. A bundle has no stock of its own — decisioning and
  stock removal go through its components — so the item there does nothing but
  name the invoice line. Which one to pick follows from how you make it:
  assembling **to stock** through a made-items sheet, use the assembled
  inventory item, and the invoice relieves it; assembling **to order**, use a
  Service item, because the components already left stock when they were
  printed and an inventory item here would take the assembled thing out as well.
* **Map an option to an item**, under *Sold as, by option* on the product. This
  is the one to reach for when a listing is several things on the books. A
  playset sold in HO and in 1:64 is two items, and the scale the buyer picked is
  what says which — so the mapping sits on the option, and a product carries as
  many as it has options worth telling apart. Several QuickBooks items on one
  product is the normal case, not an edge one.

  A mapping can pin **more than one option at once**, because a shop's items are
  not always split along a single one: *1:64 with the overhead loadout* can be
  its own item while *1:64* on its own is another. Add the first condition,
  press *…and another option*, then choose the item. It matches when every
  option it names is among the buyer's choices, so one pinning a single option
  still covers every combination containing it.

  Which is why **the most specific wins**: a mapping naming two options beats
  one naming a single option it contains, or a general rule could never have an
  exception and adding the exception would silently do nothing. Mappings naming
  the same number of options fall back to the order on screen, and the arrows
  are how you set it.

  The option names and values are offered from what past orders carried and what
  the listing sells, so there is nothing to type. Nothing derives these: which of
  your items a combination is sold as is a decision about your books.

  Not to be confused with the *Etsy options* rules above it, which answer a
  different question about the same option — those say what comes off the shelf
  to make the thing, these say what it is sold as. A colour usually changes the
  first and not the second; a scale usually changes both.

* **Link a variation to its own item**, where one exact combination needs
  naming. *Playset — HO and 1:64 Scale* is one bundle with a Scale variation,
  and the two scales are not the same item on anybody's books. **Every**
  variation can name its own item, whatever the product is and whether or not
  it has been given a product of its own, and the most specific answer wins:

  > the variation's item → an item mapped to an option the buyer chose →
  > the item of the product the line resolved to → the fallback from Settings.

  The same answer decides what a printed unit is taken out of stock from. A unit
  billed as one item and drawn down from another would be two mistakes that look
  like one, so there is only ever one lookup.

  Two gates used to stand in the way of that, both resting on the same
  assumption. Variations were hidden on a bundle, because a bundle's options
  change its BOM — which is what option rules are for — and a variation's other
  jobs, picking a print file and carrying its own stock, are things a bundle
  does not do. And a variation promoted to its own product had no item field,
  on the grounds that the item belongs on that product. It still can go there,
  and a variation left blank uses it — the screen says so, naming it — but
  "go and edit a different product" is a worse answer than a field already in
  front of you, and a shop may well bill a combination as something other than
  what it makes.
* **Set the fallback item** once, and every such line goes on it.

The Products list marks a product **no QuickBooks item** only when it has none
*anywhere* and is something the shop sells — linked to an Etsy listing. A
product billed by option shows **N by option** instead, because that is having
items assigned: it is how a listing sold in several scales is invoiced at all. Components reached
through a bundle's BOM are not marked: nothing invoices them on their own, so
having no item is ordinary rather than a gap. Finding this out on the list is
better than finding it out at the moment of invoicing.

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
4. **Wix** — optional, and skippable like every other connection. Paste an API
   key made in the Wix dashboard under **Settings → API Keys** with the Wix
   Stores or eCommerce read permissions, plus the **site ID** from the site's
   dashboard URL. No OAuth and so no callback: PrintFlow is one shop's software
   on one shop's machine, an API key is made in a minute, and it cannot expire
   out from under a poll at three in the morning. Saving validates the key by
   making the same call the poll makes, so a key that passes has been shown to
   read this site's orders. Set an import cutoff on the panel so an established
   site's back catalogue does not land on the board.
5. **QuickBooks Online** — paste your Intuit app's client ID and secret; the
   app runs the OAuth flow and stores the realm ID. Access tokens refresh
   silently.
6. **Bambuddy** — base URL + API key. Validated by fetching the instance's
   OpenAPI document (version is logged) and listing the printers it finds.
7. **ShipStation** — API key + secret. Validated by listing stores; you pick
   the one that receives your Etsy orders.
8. **Poll intervals** — defaults pre-filled (Etsy 5 min, Wix 5 min, Bambuddy
   2 min, ShipStation 10 min).

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

**Settings → Backup gives you one file that is the whole shop**, and the
Restore panel below it puts one back. Nothing to schedule, no volumes to name,
no `pg_dump` to get right — press the button, keep the file somewhere other
than this machine. A backup on the machine that died is not a backup.

The file carries both halves, which is the part a hand-rolled dump usually gets
wrong. The database holds the orders, products, plates, money and the encrypted
credentials; the data directory holds the key those credentials are encrypted
*with*. Either alone restores to nothing useful — a database with no key comes
up unable to talk to Etsy, and a key with no database comes up empty.

**A passphrase is offered and worth using.** Without one, anybody holding the
file holds your Etsy, QuickBooks and ShipStation credentials in readable form,
because the key to them is in the same archive. With one, the whole archive is
sealed with scrypt and there is no way back into it if the passphrase is lost —
which is the trade, stated on the screen.

**One case where the file is deliberately incomplete**, and PrintFlow says so
before you press the button: if `SECRET_KEY` is set in the environment rather
than generated into the data volume, it is not PrintFlow's to save. The archive
then holds credentials nothing in it can decrypt, and the restore looks fine
until every integration fails at once. Keep that value with the backup.

#### Restoring

Restore reads the file and describes it — when it was taken, how many orders
and products are in it, whether it carries the key — *before* replacing
anything. On an install that already has data, the confirmation is the word
"restore" typed out, because this deletes every order, product and credential
here and there is no undo.

The whole database side is one transaction, so a restore that fails leaves what
was there intact. That matters most precisely when somebody is restoring
because something has already gone wrong today.

**A failed restore says what failed and where.** "Internal Server Error" is the
same five words whether the file was corrupt, the database refused a row, or
the data volume is read-only — and those have three completely different fixes.
So a restore that goes wrong names the step it was on — *reading the backup
file*, *replacing the database*, *writing the data directory* — quotes what
actually went wrong, and states plainly that nothing was changed. The full
traceback goes to the container log (`docker compose logs app`).

A backup from an *older* PrintFlow restores fine: migrations here only add
columns, so the old rows load and the new columns take their defaults. A backup
from a *newer* one is refused rather than loaded with its extra columns
silently dropped — update PrintFlow first, then restore.

Rows are loaded in dependency order, and within a table in *parent-first*
order. That second part is not decoration: a product variant names its master
product and a bundle's component line names the ordered line it came from, so
two tables point at themselves. Postgres checks a foreign key the moment the
row lands rather than at the end of the transaction, and a plain `SELECT`
returns rows in whatever order the heap has them in — which, after a row has
been updated, is *not* the order they were created in. A shop with no bundles
and no variants would restore perfectly while a shop with either failed, on row
order nobody chose.

#### Restoring onto a fresh machine

**The setup wizard's first step offers Restore from a backup**, before you
create an account. That is deliberate: somebody rebuilding onto a new machine
has no account on it, and making them create one first creates an account the
restore immediately throws away — along with the password they just chose.
After restoring, sign in with the account from the backup.

That door is unauthenticated, and it is open only while the install has no
admin account — the same gate the wizard's own "create the admin" step sits
behind. Once somebody owns an install, restoring over it takes signing in.

#### If you would rather do it yourself

The two Docker volumes are still there: `printflow-db` (all the data) and
`printflow-data` (the encryption key and the certificate). Back up both or
neither; the key without the database restores nothing, and the database
without the key restores a shop that cannot log in to anything.

## The board

`New → In Production → Assembly → Ready to Ship → Shipped → Complete`

Cards are moved by whoever is working the board, by dragging or from the
drawer — see [The board, and who moves the cards](#the-board-and-who-moves-the-cards)
for why, and for the one exception. Manual overrides live in the card menu
(mark a line printed for an off-Bambuddy print, skip stock and print anyway,
cancel a line) and **every override writes an audit row**.

- **New** — ingested, decisions made, nothing dispatched yet. Orders with an
  no product stay here with a red badge so the fix path stays visible; the
  fix is "link product" from the order drawer, which re-runs intake for that
  line.
- **In Production** — at least one line dispatched to Bambuddy. A failed plate
  holds the line here and flags it red with a one-click Re-queue.
- **Assembly** — production finished but the order contains a bundle, which
  needs a manual check-off per bundle. Orders without bundles skip this column.
- **Ready to Ship** — every line ready. The Create Label button goes live.
- **Shipped** — label bought; the tracking number is a link to the carrier, and
  what the label cost is shown beside it. Parcels are checked periodically and
  the card says what the carrier last reported.
- **Complete** — the carrier said it arrived. The one column a card reaches on
  its own, and the only one it leaves the board from: two days after it lands
  here the board stops drawing it. The order itself stays in the Orders tab
  with everything it ever had.

Other screens: **Products** (CRUD, QBO item picker, Bambuddy file picker,
printer models, BOM editor), [**Printers**](#printers) (the farm, live, with
every plate grouped under the machine it went to, a page per machine showing
everything it reports, and a way to print any file on it), **Sync Log**
(background runs + audit trail), **Settings**.

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
| Wix order poll             | 5 min     | Its own job, not a branch of Etsy's — the two channels must fail independently |
| Bambuddy status reconcile  | 2 min     | Dispatches pending plates, advances jobs, and books the stock a finished print used — the one background write to your books, and switchable off |
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
   `/openapi.json`. Only used when the other two say nothing. There is
   deliberately no default for a **printer control**: a guessed listing path
   that 404s costs one failed read, while a guessed control path is a POST at a
   machine mid-print. Nothing discovered, no button.
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
breaking a poll. The `print_options` JSON of whichever print file the plate
ended up using is merged into the queue request as-is.

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

No writes to any sales channel. In QuickBooks, only made-items sheets — no invoices, bills,
journal entries or other accounting features. No multi-user roles.
No multi-level BOMs (a bundle may not contain a bundle — this is validated). No
automatic label purchase. No filament or spool tracking — Bambuddy owns that.
