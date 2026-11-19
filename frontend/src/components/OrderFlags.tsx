import { useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type { FlagColor, Order, OrderFlag } from '../lib/types'
import { Alert, Button, Modal, cx, inputClass } from './ui'

/** The shop's own two marks on an order: a note, and some flags.
 *
 * Everything else an order carries is a fact some system reported — what
 * Etsy sold, what QuickBooks was told, what the carrier last said. These two
 * are the only things on it a person wrote, which is why they are editable
 * from the card itself rather than three clicks into a drawer: the moment
 * somebody knows "this is the rush one" is the moment they are looking at
 * the board.
 */

/* Every colour a flag can be, written out.
 *
 * Written out, and not built from a template string, because Tailwind builds
 * the classes it can see in the source. `bg-${color}-100` is a class nobody
 * generated and a flag that draws as nothing at all, which looks like the
 * flag failed to save. The server keeps its own copy of this list and will
 * not store a colour that is not on it. */
export const FLAG_CLASSES: Record<FlagColor, string> = {
  slate: 'bg-slate-100 text-slate-800 ring-slate-300',
  red: 'bg-red-100 text-red-800 ring-red-300',
  amber: 'bg-amber-100 text-amber-900 ring-amber-300',
  emerald: 'bg-emerald-100 text-emerald-900 ring-emerald-300',
  sky: 'bg-sky-100 text-sky-900 ring-sky-300',
  violet: 'bg-violet-100 text-violet-800 ring-violet-300',
  pink: 'bg-pink-100 text-pink-900 ring-pink-300',
}

/* The same colours as a solid dot, for the Settings picker where the thing
   being chosen is the colour itself rather than a word wearing it. */
export const FLAG_DOTS: Record<FlagColor, string> = {
  slate: 'bg-slate-500',
  red: 'bg-red-500',
  amber: 'bg-amber-500',
  emerald: 'bg-emerald-600',
  sky: 'bg-sky-500',
  violet: 'bg-violet-500',
  pink: 'bg-pink-500',
}

export function FlagPill({
  flag,
  className,
}: {
  flag: OrderFlag
  className?: string
}) {
  return (
    <span
      className={cx(
        'inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1',
        FLAG_CLASSES[flag.color] ?? FLAG_CLASSES.slate,
        className,
      )}
    >
      {flag.label}
    </span>
  )
}

/** The flags an order carries, drawn.
 *
 * Ids the shop no longer has are dropped rather than drawn as themselves:
 * deleting a flag in Settings is a decision about the shop's vocabulary, and
 * a card that answered it by showing `waiting-on-buyer` in raw would be
 * arguing with the decision.
 */
export function OrderFlagPills({
  flags,
  options,
  className,
}: {
  flags: string[]
  options: OrderFlag[]
  className?: string
}) {
  const known = options.filter((option) => flags.includes(option.id))
  if (known.length === 0) return null
  return (
    <div className={cx('flex flex-wrap gap-1', className)}>
      {known.map((flag) => (
        <FlagPill key={flag.id} flag={flag} />
      ))}
    </div>
  )
}

function NoteIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" aria-hidden="true">
      <path
        d="M3 2.5h10v8l-3 3H3z M13 10.5h-3v3"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
      />
      <path d="M5.5 6h5M5.5 8.5h3" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}

function FlagIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" aria-hidden="true">
      <path
        d="M4 14V2.5h8l-1.6 3 1.6 3H4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  )
}

/** The buttons that open the editor, as they sit on a card.
 *
 * Deliberately quiet: an order with nothing written on it should look like
 * an order, not like a form. They say what they are when there is something
 * to say — "Note" becomes the first words of the note — so a card with a
 * note on it reads as a card with a note on it from across the room.
 */
export function NoteAndFlagsButtons({
  order,
  onOpen,
}: {
  order: Pick<Order, 'notes' | 'flags'>
  onOpen: (tab: 'note' | 'flags') => void
}) {
  // stopPropagation everywhere: the whole card opens the drawer, and
  // reaching for the note is not asking for the drawer.
  const stop = (event: React.SyntheticEvent) => event.stopPropagation()
  const quiet =
    'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium ' +
    'text-ink-400 transition hover:bg-ink-100 hover:text-ink-700 ' +
    'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ink-500'
  return (
    <>
      <button
        type="button"
        className={quiet}
        onClick={(event) => {
          stop(event)
          onOpen('note')
        }}
        onKeyDown={stop}
        title={order.notes ? 'Edit this note' : 'Write a note on this order'}
      >
        <NoteIcon />
        {order.notes ? 'Note' : 'Add note'}
      </button>
      <button
        type="button"
        className={quiet}
        onClick={(event) => {
          stop(event)
          onOpen('flags')
        }}
        onKeyDown={stop}
        title="Flag this order"
      >
        <FlagIcon />
        {order.flags.length ? `${order.flags.length} flagged` : 'Flag'}
      </button>
    </>
  )
}

/** The note itself, on a card.
 *
 * Two lines, because a card is a card. The whole thing is in the title
 * attribute and in the drawer; this is the reminder that there is something
 * to read, not the reading.
 */
export function NotePreview({ notes }: { notes: string | null }) {
  if (!notes) return null
  return (
    <p
      className="mt-2 line-clamp-2 whitespace-pre-wrap rounded border-l-2 border-amber-300 bg-amber-50/60 py-1 pl-2 text-[11px] text-ink-700"
      title={notes}
    >
      {notes}
    </p>
  )
}

/** Write the note and set the flags, in one small dialog.
 *
 * One dialog for both, because they are one thought: somebody looking at a
 * card and deciding it needs attention wants to say why and how in the same
 * moment. Saving sends only what changed — a note that was not touched is
 * not rewritten, so two people working the same board do not overwrite each
 * other's sentences by opening a dialog and closing it.
 */
export default function OrderNoteFlagsDialog({
  order,
  options,
  focus,
  onClose,
  onSaved,
}: {
  order: Pick<Order, 'id' | 'order_number' | 'notes' | 'flags'>
  options: OrderFlag[]
  /** Which half the person reached for. Only decides what is focused. */
  focus: 'note' | 'flags'
  onClose: () => void
  onSaved: () => void | Promise<void>
}) {
  const [notes, setNotes] = useState(order.notes ?? '')
  const [flags, setFlags] = useState<string[]>(order.flags)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setNotes(order.notes ?? '')
    setFlags(order.flags)
  }, [order.id, order.notes, order.flags])

  const noteChanged = (notes.trim() || null) !== (order.notes ?? null)
  const flagsChanged =
    flags.length !== order.flags.length ||
    flags.some((id) => !order.flags.includes(id))

  const save = async () => {
    setBusy(true)
    try {
      if (noteChanged) {
        await api.patch(`/api/orders/${order.id}/notes`, { notes: notes.trim() })
      }
      if (flagsChanged) {
        await api.put(`/api/orders/${order.id}/flags`, { flags })
      }
      await onSaved()
      onClose()
    } catch (err) {
      setError(errorMessage(err))
      setBusy(false)
    }
  }

  return (
    <Modal open title={`Order #${order.order_number}`} onClose={onClose}>
      <div className="space-y-4">
        {error ? <Alert tone="error">{error}</Alert> : null}

        <div>
          <p className="mb-1.5 text-sm font-medium text-ink-700">Flags</p>
          {options.length === 0 ? (
            <p className="text-xs text-ink-500">
              This shop has no flags yet. Settings is where they are set up.
            </p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {options.map((option) => {
                const on = flags.includes(option.id)
                return (
                  <button
                    key={option.id}
                    type="button"
                    aria-pressed={on}
                    onClick={() =>
                      setFlags((was) =>
                        was.includes(option.id)
                          ? was.filter((id) => id !== option.id)
                          : // Kept in the shop's own order rather than in
                            // the order they were clicked, so one order's
                            // flags read the same way as the next one's.
                            options
                              .map((flag) => flag.id)
                              .filter((id) => id === option.id || was.includes(id)),
                      )
                    }
                    className={cx(
                      'rounded-full px-2.5 py-1 text-xs font-medium ring-1 transition',
                      'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-ink-500',
                      on
                        ? FLAG_CLASSES[option.color] ?? FLAG_CLASSES.slate
                        : 'bg-white text-ink-500 ring-ink-200 hover:text-ink-800 hover:ring-ink-300',
                    )}
                  >
                    {option.label}
                  </button>
                )
              })}
            </div>
          )}
        </div>

        <label className="block">
          <span className="mb-1 block text-sm font-medium text-ink-700">Note</span>
          <textarea
            autoFocus={focus === 'note'}
            rows={5}
            value={notes}
            maxLength={4000}
            onChange={(event) => setNotes(event.target.value)}
            placeholder="Anything the shop should remember about this order."
            className={cx(inputClass, 'resize-y')}
          />
          <span className="mt-1 block text-xs text-ink-500">
            Yours, not the channel's: nothing polled from Etsy or Wix will
            overwrite it, and moving the card does not clear it.
          </span>
        </label>

        <div className="flex items-center justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={save}
            disabled={busy || (!noteChanged && !flagsChanged)}
          >
            {busy ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
