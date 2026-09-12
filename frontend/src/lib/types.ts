export type Fulfillment = 'printed' | 'stocked' | 'bundle'

export type LineState =
  | 'unmatched'
  | 'new'
  | 'exploded'
  | 'allocated'
  | 'printing'
  | 'printed'
  | 'ready'
  | 'labeled'
  | 'shipped'
  | 'cancelled'

export type OrderStatus =
  | 'new'
  | 'in_production'
  | 'assembly'
  | 'ready_to_ship'
  | 'shipped'
  | 'complete'
  | 'cancelled'

/** What the carrier last said about a parcel, in PrintFlow's words. */
export type TrackingStatus =
  | 'unknown'
  | 'accepted'
  | 'in_transit'
  | 'delivered'
  | 'exception'

export type JobStatus = 'pending' | 'queued' | 'printing' | 'done' | 'failed' | 'cancelled'

export interface PrintJob {
  id: string
  status: JobStatus
  bambuddy_queue_id: number | null
  bambuddy_archive_id: number
  plate_number: number | null
  printer_id: number | null
  /** The file this plate is using, named for a person. A product can hold
   *  several, so its name no longer says which one is printing. */
  file_label: string | null
  /** The models that could take it. Empty means any of them. */
  printer_models: string[]
  units_expected: number
  queued_at: string | null
  completed_at: string | null
  error: string | null
  bambuddy_url?: string | null
}

export interface OrderLine {
  id: string
  parent_line_id: string | null
  product_id: string | null
  sku: string | null
  sku_raw: string | null
  name: string | null
  fulfillment: Fulfillment | null
  quantity: number
  qty_from_stock: number
  qty_to_print: number
  state: LineState
  override_state: LineState | null
  force_print: boolean
  stock_note: string | null
  variations: { name: string; value: string; free_text?: boolean }[]
  option_effects: string[]
  /** Which variation of its product this line matched, if any. */
  variation_label: string | null
  etsy_listing_id: number | null
  etsy_product_id: number | null
  /** The same idea on the other channel: which catalogue item and which
   *  variant of it Wix sold. Strings, because Wix's ids are GUIDs. */
  wix_catalog_item_id: string | null
  wix_variant_id: string | null
  assembled_at: string | null
  /** When these units were taken out of QuickBooks stock, and how many. Null
   *  for a line that has not been printed, or whose product QuickBooks does not
   *  track. */
  qbo_stock_removed_at: string | null
  qbo_stock_qty: number | null
  /** What took them out: `printed`, or `assembled` for a component consumed
   *  into a bundle. Undoing an assembly puts back only what it took. */
  qbo_stock_reason: 'printed' | 'assembled' | null
  /** Why the last removal did not happen. A removal that quietly failed is the
   *  failure worth designing against, so the line carries its own answer. */
  qbo_stock_error: string | null
  is_bundle: boolean
  print_jobs: PrintJob[]
  children: OrderLine[]
}

export interface OrderSummary {
  line_count: number
  unmatched_count: number
  failed_job_count: number
  needs_attention: boolean
  units_from_stock: number
  units_to_print: number
  pending_assembly: { line_id: string; sku: string | null }[]
}

export type OrderSource = 'etsy' | 'wix' | 'manual'

export interface Order {
  id: string
  order_number: string
  /** Which shop window sold it. Everything past intake treats an order the
   *  same whichever channel it came from; this is for the person reading the
   *  card, who needs to know where to go and look when a buyer asks. */
  source: OrderSource
  /** Null on a Wix order — Etsy's receipt number is Etsy's alone. */
  etsy_receipt_id: number | null
  buyer_name: string | null
  placed_at: string | null
  /** The column this card sits in. Only ever set by a person. */
  status: OrderStatus
  status_note: string | null
  /** Where the roll-up would put it, when it has anything useful to say. Shown
   *  only when it disagrees, and never acted on. Null for an order whose lines
   *  are all cancelled: cancelling is decided, not observed. */
  suggested_status: OrderStatus | null
  tracking_number: string | null
  /** Where clicking the tracking number goes. Null when PrintFlow does not
   *  know that carrier's page — a link to the wrong one looks like an answer. */
  tracking_url: string | null
  tracking_status: TrackingStatus | null
  /** The carrier's own sentence — "Left with an individual at 2:03pm". */
  tracking_detail: string | null
  tracking_checked_at: string | null
  /** When the carrier said it arrived. */
  delivered_at: string | null
  /** When the card entered Complete, however it got there. The 48-hour clock
   *  that takes it off the board runs from here. */
  completed_at: string | null
  label_created_at: string | null
  /** What the label cost, postage and insurance together — a decimal string,
   *  because this is money and JSON's only number cannot hold 7.41 exactly.
   *  Null for a label bought before PrintFlow recorded it. */
  label_cost: string | null
  label_currency: string | null
  /** The currency every figure below is in — Etsy sends them all in the shop's. */
  currency: string | null
  /** What the buyer paid, all in, and how it was made up. Decimal strings. */
  revenue: string | null
  items_total: string | null
  shipping_total: string | null
  tax_total: string | null
  discount_total: string | null
  /** What selling it cost, as positive amounts taken off the top. Null until
   *  the fee sweep has found them — Etsy charges after the sale, not during. */
  etsy_fees: string | null
  marketing_fees: string | null
  processing_fees: string | null
  /** Where those three came from: swept out of Etsy's ledger, or typed by a
   *  person because it had not settled yet. The sweep leaves a manual figure
   *  alone, so the screen has to say which it is. */
  fees_source: 'etsy' | 'manual' | null
  /** Revenue less every fee and the label. Null when revenue is unknown. */
  net: string | null
  finance_synced_at: string | null
  /** The QuickBooks invoice, when one has been raised. `doc_number` is what a
   *  person searches for in QuickBooks; the id is what stops a second one. */
  qbo_invoice_id: string | null
  qbo_invoice_doc_number: string | null
  qbo_invoice_at: string | null
  qbo_invoice_total: string | null
  qbo_invoice_error: string | null
  /** What the order cost, expensed into QuickBooks. Two bills that arrive at
   *  different times from different people — the carrier's postage and Etsy's
   *  cut — so two documents, each once-only and each removable on its own. */
  qbo_shipping_expense_id: string | null
  qbo_shipping_expense_at: string | null
  qbo_shipping_expense_total: string | null
  qbo_shipping_expense_error: string | null
  qbo_fee_expense_id: string | null
  qbo_fee_expense_at: string | null
  qbo_fee_expense_total: string | null
  qbo_fee_expense_error: string | null
  shipstation_order_id: number | null
  summary: OrderSummary
  lines: OrderLine[]
  carrier_code?: string | null
  service_code?: string | null
  has_label_pdf?: boolean
  /** Whether there is a channel payload behind this order to look at. False
   *  for one typed in by hand: nothing sent it, so there is nothing to read. */
  raw_available?: boolean
  bambuddy_base_url?: string | null
  /** Where it is going, as Etsy sent it. Drawer only. */
  ship_to?: {
    name?: string
    first_line?: string
    second_line?: string
    city?: string
    state?: string
    zip?: string
    country?: string
    formatted?: string
    email?: string
  } | null
  /** Every fee line behind the totals. Drawer only. */
  fee_lines?: {
    kind: 'etsy' | 'marketing' | 'processing'
    description: string | null
    amount: string
    ledger_entry_id?: number | string | null
    created_at?: number | null
  }[]
}

export interface IntegrationStatus {
  provider: 'etsy' | 'qbo' | 'bambuddy' | 'shipstation'
  connected: boolean
  undecryptable: boolean
  connected_at: string | null
  last_ok_at: string | null
  last_error: string | null
  last_error_at: string | null
  detail: Record<string, any>
}

export interface BoardResponse {
  columns: { key: OrderStatus; orders: Order[]; count: number }[]
  /** Delivered orders the board has stopped drawing. They are still in the
   *  Orders tab with everything they ever had — nothing is deleted. */
  retired_from_board: number
  complete_board_hours: number
  integrations: IntegrationStatus[]
}

export interface BomEntry {
  id: string
  component_id: string
  component_sku: string | null
  component_name: string | null
  component_fulfillment: Fulfillment | null
  quantity: number
}

/** One file a product can be printed from, and the machines it is for.
 *
 *  A product has one per way it can be made — the same part sliced for each
 *  machine that can take it. Which one is used is decided when a plate is sent. */
export interface PrintFile {
  id: string
  /** Either of these identifies the file; a file-manager pick may have no id. */
  bambuddy_archive_id: number | null
  bambuddy_archive_name: string | null
  bambuddy_file_path: string | null
  /** Set when the file lives on one machine — that machine gets the plate. */
  bambuddy_printer_id: number | null
  plate_number: number
  units_per_plate: number
  print_options: Record<string, unknown>
  /** Printer models that can take this plate. Empty means any of them. */
  printer_models: string[]
}

/** An option name and the values seen for it — from past orders, or from the
 *  Etsy listing itself. `orders` is absent when it came from the listing. */
export interface ObservedOption {
  name: string
  values: { value: string; orders?: number }[]
}

export interface CatalogRow {
  listing_id: number | null
  title: string | null
  state: string | null
  url: string | null
  sku: string | null
  options: { name: string; value: string }[]
  listing_options: ObservedOption[]
  product_id: string | null
  product_sku: string | null
  product_name: string | null
  fulfillment: Fulfillment | null
  qbo_item_id: string | null
  status: 'matched' | 'linked' | 'missing' | 'no_sku'
}

export interface Catalog {
  rows: CatalogRow[]
  unused_products: { id: string; sku: string; name: string; fulfillment: Fulfillment }[]
  counts: Record<string, number>
  notes: string[]
  error: string | null
  needs_reconnect: boolean
}

export interface OptionRule {
  id: string
  option_name: string
  option_value: string
  replaces_id: string | null
  replaces_sku: string | null
  component_id: string
  component_sku: string | null
  quantity: number | null
}

export interface Product {
  id: string
  sku: string
  name: string
  fulfillment: Fulfillment
  /** Set when this product is a variant of another — "Bin, with fan". */
  parent_id: string | null
  qbo_item_id: string | null
  qbo_item_name: string | null
  active: boolean
  created_at: string
  updated_at: string
  print_files: PrintFile[]
  bom: BomEntry[]
  option_rules: OptionRule[]
  /** Which QuickBooks item each chosen option is sold as. A listing sold in
   *  several scales is several items on the books, so one product carries as
   *  many of these as it has options worth telling apart. */
  option_items: OptionItem[]
  etsy_links: EtsyLink[]
  wix_links: WixLink[]
  variations: ProductVariation[]
}

/** One buyable combination of a product's options, and what it changes.
 *
 * Orders attach to one of these automatically — by Etsy's variation id where
 * it is current, by the option values otherwise. Every override is optional;
 * null means "use the product's". */
export interface ProductVariation {
  id: string
  label: string
  options: { name: string; value: string }[]
  etsy_listing_id: number | null
  etsy_product_id: number | null
  bambuddy_archive_id: number | null
  bambuddy_archive_name: string | null
  bambuddy_file_path: string | null
  bambuddy_printer_id: number | null
  plate_number: number | null
  units_per_plate: number | null
  /** Printer models that can take this plate. Empty falls back to the product's. */
  printer_models: string[]
  qbo_item_id: string | null
  qbo_item_name: string | null
  /** The product this combination is, when it has one of its own. */
  variant_product_id: string | null
  variant_product_name: string | null
  variant_product_fulfillment: Fulfillment | null
  /** What that product is billed and drawn down against — what this variation
   *  falls back to when it names no item of its own. */
  variant_product_qbo_item_id: string | null
  variant_product_qbo_item_name: string | null
  active: boolean
}

/** An Etsy listing that resolves to this product. This is how orders match. */
/** "When the buyer picks this, bill it as that." Set by hand, never derived. */
export interface OptionItem {
  id: string
  /** Every one of these has to be among the buyer's choices for it to match,
   *  so a mapping can pin a combination — 1:64 *with* the loadout — and not
   *  only a single option. */
  options: { name: string; value: string }[]
  qbo_item_id: string
  qbo_item_name: string | null
  /** Orders mappings that are equally specific. The one pinning more options
   *  wins regardless, or a general rule could never have an exception. */
  position: number
}

export interface EtsyLink {
  id: string
  etsy_listing_id: number
  etsy_product_id: number | null
  listing_title: string | null
}

/** A Wix catalogue item that resolves to this product.
 *
 * The same idea as an Etsy link, with Wix's identifiers: GUIDs rather than
 * numbers, and `wix_catalog_item_id` is exactly what an order carries as
 * `catalogItemId` — which is what makes a link made here match one later. */
export interface WixLink {
  id: string
  wix_catalog_item_id: string
  wix_variant_id: string | null
  item_title: string | null
}

/** The Wix catalogue check: every item, and what PrintFlow thinks each is. */
export interface WixCatalog {
  items: WixCatalogItem[]
  counts: Record<string, number>
  /** How many could be linked in one press, without counting the rows. */
  proposed: number
  total: number
  error: string | null
  /** Which Stores generation this site answered on, once known. */
  api_version: string | null
}

/** One row of the Wix catalogue, lined up against the product table. */
export interface WixCatalogItem {
  wix_catalog_item_id: string
  name: string | null
  sku: string | null
  visible: boolean
  variant_count: number
  /** `sku` | `etsy_title` | `product_name` when something recognised it,
   *  `linked` when a link already exists, `missing` when nothing did. */
  status: string
  /** How it was recognised, in words, for the ones that were. */
  matched_on: string | null
  product_id: string | null
  product_sku: string | null
  product_name: string | null
  link_id: string | null
}

export interface QboItem {
  id: string
  name: string
  sku: string | null
  type: string | null
  qty_on_hand: number | null
  tracked: boolean
  /** Whether an invoice line naming this item would change quantity on hand.
   *  The income-item setting refuses one that would — the printed line has
   *  already taken those units out. */
  moves_stock?: boolean
}

export interface BambuddyArchive {
  id: number | null
  name: string | null
  created_at: string | null
  plates: number | null
  thumbnail: string | null
}

export interface BambuddyPrinter {
  id: number | null
  name: string | null
  model: string | null
  status: string | null
  online: boolean | null
}

/** One loaded spool — an AMS tray, or the only spool on a machine without one. */
export interface FilamentSpool {
  slot: number | string
  /** PLA, PETG, ABS… as the machine spells it. */
  type: string | null
  /** Which PLA — "PLA Matte", "PLA Basic" — where the build says. */
  brand: string | null
  /** The colour as a word, when the build sent one. Null when it sent a code,
   *  because the swatch already says what the code said. */
  colour: string | null
  /** The colour as something drawable, when it could be read confidently. */
  colour_hex: string | null
  remaining: number | null
}

/** One job waiting on a machine, as Bambuddy has it.
 *
 *  This is the machine's own line, not PrintFlow's list: it holds plates
 *  dispatched from orders and whatever anybody sent from Bambuddy's own screen
 *  or from Print a file. `from_order` says which is which. */
export interface QueuedJob {
  id: number | string | null
  status: string | null
  name: string | null
  file_path: string | null
  plate_number: number | null
  /** Where it sits in the line. The build's own number where it keeps one,
   *  otherwise the order the rows arrived in — which is the order it will be
   *  worked through either way. */
  position: number
  created_at: string | null
  started_at: string | null
  error: string | null
  archive_id: number | null
  printer_id: number | string | null
  /** Set when PrintFlow recognises this as one of its own plates. */
  order_number: string | null
  product_name: string | null
  print_job_id: string | null
  from_order: boolean
}

/** One machine on the farm, with whatever this Bambuddy will say about it.
 *
 *  Every reading is optional — builds differ on what they report, and a null is
 *  "not reported", which is a different thing from zero. */
export interface FarmPrinter extends BambuddyPrinter {
  /** What it is doing, in Bambu's own words — RUNNING, PAUSE, FINISH. */
  state: string | null
  /** How far through, 0–100. */
  progress: number | null
  remaining_minutes: number | null
  /** The plate on it now, as the machine names it. */
  current_file: string | null
  layer: number | null
  layers: number | null
  nozzle_temp: number | null
  nozzle_target: number | null
  bed_temp: number | null
  bed_target: number | null
  chamber_temp: number | null
  error: string | null
  /** What the machine is, beyond what it is doing. All optional — builds
   *  differ on which of these they report, and most report only some. */
  serial: string | null
  firmware: string | null
  ip: string | null
  nozzle_diameter: number | null
  nozzle_type: string | null
  wifi_signal: string | number | null
  speed_level: string | number | null
  fan_speed: number | null
  chamber_light: string | boolean | null
  door_open: string | boolean | null
  print_started_at: string | number | null
  total_print_time: number | null
  prints_completed: number | null
  /** What is loaded — one entry per AMS tray, or one for a lone spool. */
  filament: FilamentSpool[]
  /** Every field the instance sent, flattened and untouched. This is what
   *  "all available metrics" has to mean: PrintFlow cannot know what a given
   *  build reports, so the machine's own page shows the lot. */
  reported: Record<string, string | number | boolean | null>
  /** Whether PrintFlow can fetch a picture from this machine. False when the
   *  build has no camera, or keeps it somewhere PrintFlow will not follow. */
  camera: boolean
  /** A camera the build named itself. Shown as a link when it points off the
   *  Bambuddy host, since PrintFlow will not proxy it but a browser on the
   *  same network still can. */
  camera_url: string | null
  /** The plates PrintFlow sent to this machine and has not finished with. */
  plates: QueueJob[]
  /** Everything lined up on this machine, in the order it will be printed —
   *  including jobs PrintFlow did not put there. */
  queue: QueuedJob[]
  /** What this machine can be told to do. Empty when the Bambuddy build
   *  offers no controls PrintFlow could find — which is a real answer, not a
   *  failure, and means no buttons rather than broken ones. */
  controls: PrinterControl[]
}

/** One button on a machine's card.
 *
 *  Whether it exists at all comes from Bambuddy's own API document; whether it
 *  is pressable right now comes from what the machine is doing. A control that
 *  does not apply stays on the card, disabled and with `why` on it, so the row
 *  does not rearrange itself under the cursor. */
export interface PrinterControl {
  /** What to send back: POST /api/printers/{id}/control/{action}. */
  action: string
  label: string
  /** `primary` acts on the print in front of you and earns a place on the
   *  card; `more` lives behind one button, so ten machines are not fifty
   *  buttons. Anything PrintFlow does not recognise is `more`. */
  group: 'primary' | 'more'
  enabled: boolean
  /** Why not, when it is not. Null when it is. */
  why: string | null
  /** Irreversible — draw it apart from the rest. */
  danger: boolean
  /** Ask this before sending. Null for the ones that need no asking. */
  confirm: string | null
}

/** Why there are no pictures, according to the instance itself. */
export interface CameraReport {
  available: boolean
  /** The path PrintFlow would call, and where that path came from. */
  path: string
  source: string
  /** Every endpoint the instance's document mentions that sounds like a camera.
   *  Empty means this build has none, which is not a fault to fix. */
  candidates: { path: string; methods: string[] }[]
  spec_path: string | null
  printer: Record<string, unknown> | null
  probe: {
    endpoint: string
    printer?: string | null
    status?: number
    content_type?: string | null
    starts_with?: string
    looks_like_a_picture?: boolean
    error?: string
  } | null
}

/** The farm in one line, for the top of the page. */
/** Who will number the next invoice, and what that number will be.
 *
 *  QuickBooks numbers its own sales documents unless the company has been set
 *  to number them itself, in which case it assigns nothing and PrintFlow works
 *  the next one out from the most recent invoice. */
export interface InvoiceNumbering {
  numbered_by: 'quickbooks' | 'printflow' | 'unknown'
  /** The reference this invoice will carry, when PrintFlow is the one setting
   *  it. Null when QuickBooks will apply its own. */
  next: string | null
  last: string | null
  /** A sentence for the screen, whichever of the cases applies. */
  why: string
}

/** One unit that left QuickBooks stock because of an order. */
export interface StockMovement {
  line_id: string
  order_id: string
  order_number: string | null
  product: string | null
  sku: string | null
  qbo_item_id: string | null
  quantity: number | null
  removed_at: string | null
  reason: 'printed' | 'assembled' | null
  qbo_purchase_id: string | null
  /** Set when the removal was attempted and refused. Included deliberately:
   *  a removal that did not happen is the one an auditor wants to see. */
  error: string | null
}

/** What a maintenance entry says the machine is, once that entry is written. */
export type MaintenanceStatus = 'serviced' | 'ok' | 'due' | 'attention' | 'down'

/** One thing that happened to a machine, on a date, at a running total. */
export interface MaintenanceLog {
  id: string
  machine_id: string
  /** A day, not a moment: these get written up at the end of a shift. */
  logged_on: string
  /** The machine's own hour counter at the time, as a decimal string. Null
   *  where nobody read it — a real answer, and better than an invented zero. */
  hours: string | null
  status: MaintenanceStatus
  status_label: string
  notes: string | null
  actor: string | null
  created_at: string
}

/** A printer, as PrintFlow's own maintenance records know it.
 *
 *  Deliberately not a Bambuddy printer: a maintenance history has to outlive
 *  changing farm managers, re-adding a printer under a new id, and the machine
 *  leaving the farm. `bambuddy_printer_id` is an optional convenience. */
export interface Machine {
  id: string
  name: string
  model: string | null
  serial: string | null
  bambuddy_printer_id: string | null
  notes: string | null
  active: boolean
  /** Read from the newest entry rather than stored, so it cannot drift from
   *  the history under it. Null for a machine nobody has logged yet. */
  status: MaintenanceStatus | null
  status_label: string | null
  /** Whether that newest entry is the sort somebody should go and look at. */
  needs_somebody: boolean
  last_logged_on: string | null
  /** The last hour reading anybody wrote down, which is not always the newest
   *  entry — a note about a rattle need not carry the counter. */
  last_hours: string | null
  logs: MaintenanceLog[]
  created_at: string
}

export interface FarmSummary {
  machines: number
  by_state: Record<string, number>
  printing: number
  idle: number
  offline: number
  /** When the whole farm is free — the longest job left, not the shortest. */
  busy_until_minutes: number | null
  plates_open: number
  plates_waiting: number
  units_open: number
}

export interface FarmOverview {
  printers: FarmPrinter[]
  summary: FarmSummary
  /** Plates with no machine yet, or on a machine the farm no longer lists. */
  unplaced: QueueJob[]
  /** Every plate, finished ones included — the history the farm cards omit. */
  plates: QueueJob[]
  /** Bambuddy could not be reached. The plates below are still real. */
  error: string | null
  /** Whether Bambuddy said anything about what the machines are doing. */
  live: boolean
  /** Why one machine could not be asked, where the rest could. */
  detail_error: string | null
  /** Queued jobs on no machine in particular — Bambuddy sends these to
   *  whichever comes free — or on one the farm no longer lists. */
  queue_unassigned: QueuedJob[]
  /** Why the queue could not be read, where the machines could. */
  queue_error: string | null
}

/** One entry in Bambuddy's file manager, folder or file. */
export interface BambuddyFile {
  name: string
  path: string
  kind: 'folder' | 'file'
  size: number | null
  modified: string | null
  archive_id: number | null
  /** False for folders, and for files that are not something a printer takes. */
  printable: boolean
  /** The folder this sits in, as a path. "/" at the top. */
  parent: string
  depth: number
  /** A folder Bambuddy would not list. It is shown, empty, rather than dropped. */
  unreadable?: boolean
}

export interface BambuddyFileTree {
  printer_id: number | null
  files: BambuddyFile[]
  /** The walk hit its cap, so this is not the whole file manager. */
  truncated: boolean
  /** This build keeps one library for the farm; the printer only says where
   *  the plate goes, not where the file is kept. */
  shared: boolean
  printable: number
  folders: number
  /** The path actually called, after any re-discovery. */
  endpoint: string | null
  /** Rows the top-level reply held, and how many of those had a usable name.
   *  An empty tree with rows > 0 is a shape PrintFlow could not read, which is
   *  a different problem from a file manager with nothing in it. */
  root_rows: number
  root_named: number
  /** The instance answered a folder request with its root, so this is one
   *  level and the folders in it could not be opened. */
  flat: boolean
}

/** One untouched reply, for diagnosing a tree that is missing things. */
export interface BambuddyProbe {
  endpoint: string
  error?: string
  keys?: string[] | null
  kind?: string
  rows_found?: number
  total?: number | null
  body?: string
  body_truncated?: boolean
}

export interface BambuddyRawListing {
  probes: BambuddyProbe[]
}

/** One machine type on the farm, and how many of it there are. */
export interface BambuddyPrinterModel {
  model: string
  printers: number
  online: number
  names: string[]
}

export interface QueueJob extends PrintJob {
  order_id: string | null
  order_number: string | null
  buyer_name: string | null
  sku: string | null
  product_name: string | null
  created_at: string
}

export interface SyncLogEntry {
  id: string
  job: string
  started_at: string
  finished_at: string | null
  ok: boolean | null
  detail: string | null
}

export interface AuditEntry {
  id: string
  entity_type: string
  entity_id: string | null
  action: string
  detail: Record<string, unknown> | null
  actor: string | null
  created_at: string
}

export interface SetupStep {
  key: string
  label: string
  complete: boolean
}

export interface SetupStatus {
  setup_complete: boolean
  admin_exists: boolean
  steps: SetupStep[]
  integrations: IntegrationStatus[]
  poll_intervals: Record<string, number>
  public_base_url: string | null
}
