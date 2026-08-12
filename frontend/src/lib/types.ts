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
  assembled_at: string | null
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

export interface Order {
  id: string
  order_number: string
  etsy_receipt_id: number
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
  /** Revenue less every fee and the label. Null when revenue is unknown. */
  net: string | null
  finance_synced_at: string | null
  shipstation_order_id: number | null
  summary: OrderSummary
  lines: OrderLine[]
  carrier_code?: string | null
  service_code?: string | null
  has_label_pdf?: boolean
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
  etsy_links: EtsyLink[]
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
  active: boolean
}

/** An Etsy listing that resolves to this product. This is how orders match. */
export interface EtsyLink {
  id: string
  etsy_listing_id: number
  etsy_product_id: number | null
  listing_title: string | null
}

export interface QboItem {
  id: string
  name: string
  sku: string | null
  type: string | null
  qty_on_hand: number | null
  tracked: boolean
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
