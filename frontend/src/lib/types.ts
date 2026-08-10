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
  | 'cancelled'

export type JobStatus = 'pending' | 'queued' | 'printing' | 'done' | 'failed' | 'cancelled'

export interface PrintJob {
  id: string
  status: JobStatus
  bambuddy_queue_id: number | null
  bambuddy_archive_id: number
  plate_number: number | null
  printer_id: number | null
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
  label_created_at: string | null
  shipstation_order_id: number | null
  summary: OrderSummary
  lines: OrderLine[]
  carrier_code?: string | null
  service_code?: string | null
  has_label_pdf?: boolean
  bambuddy_base_url?: string | null
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

export interface PrintMapping {
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
  print_mapping: PrintMapping | null
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

/** The untouched reply from a folder listing, for diagnosing an empty tree. */
export interface BambuddyRawListing {
  endpoint: string
  keys: string[] | null
  kind: string
  rows_found: number
  body: string
  body_truncated: boolean
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
