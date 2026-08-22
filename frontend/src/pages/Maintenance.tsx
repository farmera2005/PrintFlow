import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { formatDateTime } from '../lib/format'
import type { Machine, MaintenanceLog, MaintenanceStatus } from '../lib/types'
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Modal,
  Spinner,
  cx,
  inputClass,
} from '../components/ui'

/** How each condition reads, at a glance across a shelf of machines. */
const TONE: Record<string, string> = {
  serviced: 'bg-emerald-100 text-emerald-800',
  ok: 'bg-emerald-100 text-emerald-800',
  due: 'bg-amber-100 text-amber-900',
  attention: 'bg-orange-100 text-orange-900',
  down: 'bg-red-100 text-red-800',
}

function today(): string {
  return new Date().toISOString().slice(0, 10)
}

/** A date with no clock on it. These are days, not moments. */
function day(value: string | null): string {
  if (!value) return '—'
  const parsed = new Date(`${value}T00:00:00`)
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      })
}

function hours(value: string | null): string | null {
  if (value === null) return null
  const number = Number(value)
  if (Number.isNaN(number)) return value
  // 1240 rather than 1240.00, but 1240.5 keeps its half.
  return `${number.toLocaleString(undefined, { maximumFractionDigits: 2 })} h`
}

/** The maintenance book: what has been done to each machine, and when.
 *
 *  These records are PrintFlow's own and have nothing to do with Bambuddy. A
 *  maintenance history has to outlive changing farm managers, re-adding a
 *  printer under a new id, and the machine leaving the farm altogether — and
 *  plenty of shops service printers no farm manager ever saw. Adopting a
 *  machine from the farm is offered as a shortcut to typing its name; nothing
 *  here needs it.
 */
export default function Maintenance() {
  const [machines, setMachines] = useState<Machine[] | null>(null)
  const [statuses, setStatuses] = useState<{ value: MaintenanceStatus; label: string }[]>([])
  const [error, setError] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [openId, setOpenId] = useState<string | null>(null)
  const [showRetired, setShowRetired] = useState(false)

  const load = useCallback(async () => {
    try {
      const data = await api.get<{
        machines: Machine[]
        statuses: { value: MaintenanceStatus; label: string }[]
      }>('/api/maintenance')
      setMachines(data.machines)
      setStatuses(data.statuses)
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  if (machines === null) {
    return (
      <div className="flex justify-center py-12">
        <Spinner className="h-6 w-6" />
      </div>
    )
  }

  const retired = machines.filter((row) => !row.active)
  const shown = showRetired ? machines : machines.filter((row) => row.active)
  const wanting = machines.filter((row) => row.active && row.needs_somebody)

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-4xl space-y-4">
        <header className="flex flex-wrap items-center gap-3">
          <div>
            <h1 className="text-lg font-semibold text-ink-900">Maintenance</h1>
            <p className="text-sm text-ink-600">
              What has been done to each machine, and when. PrintFlow's own
              records — nothing here depends on Bambuddy, and they outlive it.
            </p>
          </div>
          <div className="ml-auto">
            <Button variant="primary" onClick={() => setAdding(true)}>
              Add a machine
            </Button>
          </div>
        </header>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {wanting.length ? (
          <Alert tone="warning">
            {wanting.length === 1
              ? `${wanting[0].name} wants a look: ${wanting[0].status_label?.toLowerCase()}.`
              : `${wanting.length} machines want a look — they are at the top.`}
          </Alert>
        ) : null}

        {shown.length === 0 ? (
          <EmptyState
            title="No machines yet"
            description="Add the printers you service. Each one keeps its own log of hours, dates, notes and condition — kept here rather than in whatever is watching the farm this year."
          />
        ) : (
          <div className="space-y-2">
            {shown.map((machine) => (
              <MachineRow
                key={machine.id}
                machine={machine}
                statuses={statuses}
                open={openId === machine.id}
                onToggle={() =>
                  setOpenId((was) => (was === machine.id ? null : machine.id))
                }
                onChanged={load}
              />
            ))}
          </div>
        )}

        {retired.length && !showRetired ? (
          <Button size="sm" variant="ghost" onClick={() => setShowRetired(true)}>
            Show {retired.length} retired machine{retired.length === 1 ? '' : 's'}
          </Button>
        ) : null}
        {showRetired && retired.length ? (
          <Button size="sm" variant="ghost" onClick={() => setShowRetired(false)}>
            Hide retired machines
          </Button>
        ) : null}

        <AddMachine
          open={adding}
          onClose={() => setAdding(false)}
          onAdded={async (id) => {
            await load()
            setOpenId(id)
          }}
        />
      </div>
    </div>
  )
}

function MachineRow({
  machine,
  statuses,
  open,
  onToggle,
  onChanged,
}: {
  machine: Machine
  statuses: { value: MaintenanceStatus; label: string }[]
  open: boolean
  onToggle: () => void
  onChanged: () => Promise<void> | void
}) {
  return (
    <Card className={cx('overflow-hidden', !machine.active && 'opacity-70')}>
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full flex-wrap items-center gap-2 p-3 text-left hover:bg-ink-50"
      >
        <span className="font-medium text-ink-900">{machine.name}</span>
        {machine.model ? <Badge>{machine.model}</Badge> : null}
        {machine.status ? (
          <Badge className={TONE[machine.status] ?? ''}>{machine.status_label}</Badge>
        ) : (
          <Badge className="bg-ink-100 text-ink-600">no entries yet</Badge>
        )}
        {!machine.active ? (
          <Badge className="bg-ink-100 text-ink-600">retired</Badge>
        ) : null}
        <span className="ml-auto text-xs text-ink-500">
          {machine.last_hours ? `${hours(machine.last_hours)} · ` : ''}
          {machine.last_logged_on ? day(machine.last_logged_on) : 'never logged'}
          {machine.logs.length
            ? ` · ${machine.logs.length} entr${machine.logs.length === 1 ? 'y' : 'ies'}`
            : ''}
        </span>
      </button>

      {open ? (
        <MachineBook machine={machine} statuses={statuses} onChanged={onChanged} />
      ) : null}
    </Card>
  )
}

function MachineBook({
  machine,
  statuses,
  onChanged,
}: {
  machine: Machine
  statuses: { value: MaintenanceStatus; label: string }[]
  onChanged: () => Promise<void> | void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const blank = {
    logged_on: today(),
    hours: '',
    status: 'serviced' as MaintenanceStatus,
    notes: '',
  }
  const [draft, setDraft] = useState(blank)

  const write = async (path: string, method: 'post' | 'patch' | 'delete', body?: unknown) => {
    setBusy(true)
    setError(null)
    try {
      if (method === 'post') await api.post(path, body)
      else if (method === 'patch') await api.patch(path, body)
      else await api.del(path)
      await onChanged()
      return true
    } catch (err) {
      setError(errorMessage(err))
      return false
    } finally {
      setBusy(false)
    }
  }

  const addEntry = async () => {
    if (await write(`/api/maintenance/machines/${machine.id}/logs`, 'post', draft)) {
      setDraft(blank)
    }
  }

  return (
    <div className="space-y-3 border-t border-ink-200 p-3">
      {machine.notes ? (
        <p className="rounded-md bg-ink-50 p-2 text-sm text-ink-700">{machine.notes}</p>
      ) : null}

      {/* The date box needs room for a picker icon as well as the date; at
          7rem the browser clips its own control. */}
      <div className="grid gap-2 sm:grid-cols-[9.5rem_7rem_1fr_auto] sm:items-end">
        <Field label="Date">
          <input
            type="date"
            className={inputClass}
            value={draft.logged_on}
            disabled={busy}
            onChange={(e) => setDraft((was) => ({ ...was, logged_on: e.target.value }))}
          />
        </Field>
        <Field label="Hours">
          <input
            className={inputClass}
            inputMode="decimal"
            placeholder="1240"
            value={draft.hours}
            disabled={busy}
            onChange={(e) => setDraft((was) => ({ ...was, hours: e.target.value }))}
          />
        </Field>
        <Field label="Notes">
          <input
            className={inputClass}
            placeholder="Nozzle changed, belts tensioned…"
            value={draft.notes}
            disabled={busy}
            onChange={(e) => setDraft((was) => ({ ...was, notes: e.target.value }))}
            onKeyDown={(e) => {
              if (e.key === 'Enter') addEntry()
            }}
          />
        </Field>
        <Field label="Status">
          <select
            className={inputClass}
            value={draft.status}
            disabled={busy}
            onChange={(e) =>
              setDraft((was) => ({ ...was, status: e.target.value as MaintenanceStatus }))
            }
          >
            {statuses.map((row) => (
              <option key={row.value} value={row.value}>
                {row.label}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" variant="primary" disabled={busy} onClick={addEntry}>
          {busy ? 'Saving…' : 'Add entry'}
        </Button>
        <span className="text-xs text-ink-500">
          Hours are the machine's own counter, not how long the job took — that
          is what makes the next service predictable.
        </span>
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}

      {machine.logs.length ? (
        <div className="divide-y divide-ink-100">
          {machine.logs.map((entry) => (
            <LogRow
              key={entry.id}
              machine={machine}
              entry={entry}
              busy={busy}
              onDelete={() =>
                write(
                  `/api/maintenance/machines/${machine.id}/logs/${entry.id}`,
                  'delete',
                )
              }
            />
          ))}
        </div>
      ) : (
        <p className="text-sm text-ink-500">
          Nothing logged for this machine yet.
        </p>
      )}

      <div className="flex flex-wrap gap-2 border-t border-ink-100 pt-2">
        <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
          Edit machine
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={busy}
          onClick={() =>
            write(`/api/maintenance/machines/${machine.id}`, 'patch', {
              active: !machine.active,
            })
          }
          title={
            machine.active
              ? 'Keeps its history and takes it out of the way'
              : 'Put it back in the list'
          }
        >
          {machine.active ? 'Retire' : 'Un-retire'}
        </Button>
      </div>

      <EditMachine
        machine={machine}
        open={editing}
        onClose={() => setEditing(false)}
        onChanged={onChanged}
      />
    </div>
  )
}

function LogRow({
  machine,
  entry,
  busy,
  onDelete,
}: {
  machine: Machine
  entry: MaintenanceLog
  busy: boolean
  onDelete: () => void
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2 text-sm">
      <span className="w-28 shrink-0 text-ink-600">{day(entry.logged_on)}</span>
      <span className="w-20 shrink-0 tabular-nums text-ink-600">
        {hours(entry.hours) ?? '—'}
      </span>
      <Badge className={TONE[entry.status] ?? ''}>{entry.status_label}</Badge>
      <span className="min-w-0 flex-1 text-ink-800">{entry.notes ?? ''}</span>
      {entry.actor ? (
        <span className="text-xs text-ink-400">{entry.actor}</span>
      ) : null}
      <button
        type="button"
        className="text-xs text-ink-400 underline hover:text-red-700"
        disabled={busy}
        onClick={() => {
          if (
            window.confirm(
              `Delete this entry from ${machine.name}'s log? ` +
                'It stays in the audit log and nowhere else.',
            )
          ) {
            onDelete()
          }
        }}
      >
        Delete
      </button>
    </div>
  )
}

function AddMachine({
  open,
  onClose,
  onAdded,
}: {
  open: boolean
  onClose: () => void
  onAdded: (id: string) => void | Promise<void>
}) {
  const [draft, setDraft] = useState({ name: '', model: '', serial: '', notes: '' })
  const [offered, setOffered] = useState<
    { bambuddy_printer_id: string; name: string; model: string | null }[]
  >([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Printers the farm knows about that have no machine here. A shortcut to
  // typing a name, not a source of truth: a farm that cannot be reached simply
  // offers nothing, and the form still works.
  useEffect(() => {
    if (!open) return
    api
      .get<{ printers: typeof offered }>('/api/maintenance/adoptable')
      .then((data) => setOffered(data.printers))
      .catch(() => setOffered([]))
  }, [open])

  const create = async (body: Record<string, unknown>) => {
    setBusy(true)
    setError(null)
    try {
      const made = await api.post<{ id: string }>('/api/maintenance/machines', body)
      setDraft({ name: '', model: '', serial: '', notes: '' })
      onClose()
      await onAdded(made.id)
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} title="Add a machine" onClose={onClose}>
      {offered.length ? (
        <div className="mb-3">
          <p className="text-xs text-ink-500">
            On the farm and not in this book yet:
          </p>
          <div className="mt-1 flex flex-wrap gap-1.5">
            {offered.map((printer) => (
              <Button
                key={printer.bambuddy_printer_id}
                size="sm"
                disabled={busy}
                onClick={() =>
                  create({
                    name: printer.name,
                    model: printer.model,
                    bambuddy_printer_id: printer.bambuddy_printer_id,
                  })
                }
              >
                {printer.name}
              </Button>
            ))}
          </div>
        </div>
      ) : null}

      <div className="space-y-2">
        <Field label="Name" hint="What the shop calls it.">
          <input
            className={inputClass}
            value={draft.name}
            disabled={busy}
            onChange={(e) => setDraft((was) => ({ ...was, name: e.target.value }))}
          />
        </Field>
        <div className="grid gap-2 sm:grid-cols-2">
          <Field label="Model">
            <input
              className={inputClass}
              placeholder="H2D"
              value={draft.model}
              disabled={busy}
              onChange={(e) => setDraft((was) => ({ ...was, model: e.target.value }))}
            />
          </Field>
          <Field label="Serial" hint="The one identifier that is the machine's own.">
            <input
              className={inputClass}
              value={draft.serial}
              disabled={busy}
              onChange={(e) => setDraft((was) => ({ ...was, serial: e.target.value }))}
            />
          </Field>
        </div>
        <Field label="Notes" hint="Standing notes about the machine — the modification, the quirk. Log entries go on its page.">
          <textarea
            className={cx(inputClass, 'h-20')}
            value={draft.notes}
            disabled={busy}
            onChange={(e) => setDraft((was) => ({ ...was, notes: e.target.value }))}
          />
        </Field>
      </div>

      {error ? (
        <div className="mt-2">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}

      <div className="mt-4 flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="primary"
          disabled={busy || !draft.name.trim()}
          onClick={() => create(draft)}
        >
          {busy ? 'Adding…' : 'Add machine'}
        </Button>
      </div>
    </Modal>
  )
}

function EditMachine({
  machine,
  open,
  onClose,
  onChanged,
}: {
  machine: Machine
  open: boolean
  onClose: () => void
  onChanged: () => Promise<void> | void
}) {
  const [draft, setDraft] = useState({
    name: machine.name,
    model: machine.model ?? '',
    serial: machine.serial ?? '',
    notes: machine.notes ?? '',
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setDraft({
      name: machine.name,
      model: machine.model ?? '',
      serial: machine.serial ?? '',
      notes: machine.notes ?? '',
    })
  }, [machine.id, machine.name, machine.model, machine.serial, machine.notes])

  const run = async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
      await onChanged()
      onClose()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open={open} title={machine.name} onClose={onClose}>
      <div className="space-y-2">
        <Field label="Name">
          <input
            className={inputClass}
            value={draft.name}
            disabled={busy}
            onChange={(e) => setDraft((was) => ({ ...was, name: e.target.value }))}
          />
        </Field>
        <div className="grid gap-2 sm:grid-cols-2">
          <Field label="Model">
            <input
              className={inputClass}
              value={draft.model}
              disabled={busy}
              onChange={(e) => setDraft((was) => ({ ...was, model: e.target.value }))}
            />
          </Field>
          <Field label="Serial">
            <input
              className={inputClass}
              value={draft.serial}
              disabled={busy}
              onChange={(e) => setDraft((was) => ({ ...was, serial: e.target.value }))}
            />
          </Field>
        </div>
        <Field label="Notes">
          <textarea
            className={cx(inputClass, 'h-20')}
            value={draft.notes}
            disabled={busy}
            onChange={(e) => setDraft((was) => ({ ...was, notes: e.target.value }))}
          />
        </Field>
      </div>

      {machine.bambuddy_printer_id ? (
        <p className="mt-2 text-xs text-ink-500">
          Linked to farm printer{' '}
          <span className="font-mono">{machine.bambuddy_printer_id}</span>. Only a
          convenience — this book does not need it, and nothing breaks if that
          printer goes away.
        </p>
      ) : null}
      <p className="mt-1 text-xs text-ink-400">
        Added {formatDateTime(machine.created_at)}
      </p>

      {error ? (
        <div className="mt-2">
          <Alert tone="error">{error}</Alert>
        </div>
      ) : null}

      <div className="mt-4 flex flex-wrap justify-end gap-2">
        <Button
          variant="danger"
          disabled={busy}
          onClick={() => {
            if (
              window.confirm(
                `Delete ${machine.name} and all ${machine.logs.length} of its log ` +
                  'entries? Retiring it instead keeps the history.',
              )
            ) {
              run(() => api.del(`/api/maintenance/machines/${machine.id}`))
            }
          }}
        >
          Delete
        </Button>
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          variant="primary"
          disabled={busy || !draft.name.trim()}
          onClick={() => run(() => api.patch(`/api/maintenance/machines/${machine.id}`, draft))}
        >
          {busy ? 'Saving…' : 'Save'}
        </Button>
      </div>
    </Modal>
  )
}
