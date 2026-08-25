import { useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { SOURCE_LABELS, formatMoney } from '../lib/format'
import type { Order } from '../lib/types'
import { Badge, cx } from './ui'

/** Raise this order's QuickBooks invoice, from wherever the order is listed.
 *
 *  The same control on the board and in the Orders list, because "has this been
 *  invoiced" is a question asked of a whole column at once and the answer is
 *  worth reading without opening anything.
 *
 *  An invoice is a document in somebody's books, so it is a button rather than
 *  something that happens on its own, and it asks first. Once raised it becomes
 *  a badge naming the QuickBooks document number — which is what a person
 *  searches for in QuickBooks, and not the same as PrintFlow's order number.
 *
 *  It lives inside a card that is itself clickable, so every event it handles
 *  is stopped from bubbling: invoicing an order is not opening it.
 */
export default function InvoiceAction({
  order,
  onChanged,
  className,
}: {
  order: Order
  onChanged: () => void
  className?: string
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (order.qbo_invoice_id) {
    return (
      <Badge
        className={cx('bg-emerald-100 text-emerald-800 ring-emerald-300', className)}
        title={
          order.qbo_invoice_total
            ? `Invoiced ${formatMoney(order.qbo_invoice_total, order.currency)} in QuickBooks`
            : 'Invoiced in QuickBooks'
        }
      >
        Invoice {order.qbo_invoice_doc_number ?? order.qbo_invoice_id}
      </Badge>
    )
  }

  const create = async () => {
    if (
      !window.confirm(
        `Create a QuickBooks invoice for order ${order.order_number}? It bills ` +
          "the buyer's name and address from this order, at the prices " +
          `${SOURCE_LABELS[order.source]} recorded, each line against its own ` +
          'QuickBooks item.',
      )
    )
      return
    setBusy(true)
    setError(null)
    try {
      await api.post(`/api/orders/${order.id}/invoice`)
      onChanged()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <button
        type="button"
        disabled={busy}
        onClick={(event) => {
          event.stopPropagation()
          create()
        }}
        onKeyDown={(event) => event.stopPropagation()}
        className={cx(
          'rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-ink-300 text-ink-600 hover:bg-ink-100 disabled:opacity-50',
          className,
        )}
        title="Raise this order's QuickBooks invoice"
      >
        {busy ? 'Invoicing…' : 'Invoice'}
      </button>
      {error ? <span className="text-xs text-red-700">{error}</span> : null}
    </>
  )
}
