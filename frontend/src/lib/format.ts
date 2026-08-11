import type { JobStatus, LineState, OrderStatus } from './types'

/** Every column an order can be in, in the order the board shows them. */
export const ORDER_STATUSES: OrderStatus[] = [
  'new',
  'in_production',
  'assembly',
  'ready_to_ship',
  'shipped',
  'cancelled',
]

/** Money, as sent: a decimal string, because JSON's only number cannot hold
 *  7.41 exactly and this is a figure somebody will reconcile against a bill.
 *  Anything unparseable is shown as it arrived rather than as NaN. */
export function formatMoney(amount: string | null, currency?: string | null): string {
  if (amount === null || amount === undefined || amount === '') return '—'
  const value = Number(amount)
  if (!Number.isFinite(value)) return String(amount)
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: currency || 'USD',
    }).format(value)
  } catch {
    // An unknown currency code is not a reason to show nothing.
    return `${value.toFixed(2)} ${currency ?? ''}`.trim()
  }
}

export const COLUMN_LABELS: Record<OrderStatus, string> = {
  new: 'New',
  in_production: 'In Production',
  assembly: 'Assembly',
  ready_to_ship: 'Ready to Ship',
  shipped: 'Shipped',
  cancelled: 'Cancelled',
}

export const LINE_STATE_LABELS: Record<LineState, string> = {
  unmatched: 'No product',
  new: 'New',
  exploded: 'Bundle',
  allocated: 'From stock',
  printing: 'Printing',
  printed: 'Printed',
  ready: 'Ready',
  labeled: 'Labeled',
  shipped: 'Shipped',
  cancelled: 'Cancelled',
}

export const LINE_STATE_CLASSES: Record<LineState, string> = {
  unmatched: 'bg-red-100 text-red-800 ring-red-300',
  new: 'bg-slate-100 text-slate-700 ring-slate-300',
  exploded: 'bg-violet-100 text-violet-800 ring-violet-300',
  allocated: 'bg-sky-100 text-sky-800 ring-sky-300',
  printing: 'bg-amber-100 text-amber-900 ring-amber-300',
  printed: 'bg-emerald-100 text-emerald-800 ring-emerald-300',
  ready: 'bg-emerald-100 text-emerald-800 ring-emerald-300',
  labeled: 'bg-indigo-100 text-indigo-800 ring-indigo-300',
  shipped: 'bg-indigo-100 text-indigo-800 ring-indigo-300',
  cancelled: 'bg-slate-200 text-slate-500 ring-slate-300 line-through',
}

export const JOB_STATUS_CLASSES: Record<JobStatus, string> = {
  pending: 'bg-slate-100 text-slate-700 ring-slate-300',
  queued: 'bg-sky-100 text-sky-800 ring-sky-300',
  printing: 'bg-amber-100 text-amber-900 ring-amber-300',
  done: 'bg-emerald-100 text-emerald-800 ring-emerald-300',
  failed: 'bg-red-100 text-red-800 ring-red-300',
  cancelled: 'bg-slate-200 text-slate-500 ring-slate-300',
}

export const PROVIDER_LABELS: Record<string, string> = {
  etsy: 'Etsy',
  qbo: 'QuickBooks Online',
  bambuddy: 'Bambuddy',
  shipstation: 'ShipStation',
}

export function formatAge(iso: string | null): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const minutes = Math.floor((Date.now() - then) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 48) return `${hours}h`
  return `${Math.floor(hours / 24)}d`
}

export function formatDateTime(iso: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}
