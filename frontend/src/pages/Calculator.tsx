import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { formatMoney } from '../lib/format'
import { Alert, Button, Card, Field, Spinner, cx, inputClass } from '../components/ui'

/** What a printed part costs to make.
 *
 * The question a shop asks before it prices anything. Five costs kept apart on
 * purpose — a total nobody can take apart is a total nobody argues with, and
 * the argument is the point: a part that looks expensive is usually expensive
 * for one reason, and the breakdown says which.
 *
 * The arithmetic is on the server, in Decimal, next to every other figure that
 * is money here. A cost worked out in the browser and again on the server
 * eventually disagrees with itself, and this one is meant to be defended.
 */

interface FilamentRow {
  name: string
  price_per_kg: string
}

interface Settings {
  machine_per_hour: string
  printer_watts: string
  electricity_per_kwh: string
  labour_per_hour: string
  failure_percent: string
  markup_percent: string
  filaments: FilamentRow[]
}

interface Line {
  key: string
  label: string
  amount: string
  detail: string | null
}

interface Estimate {
  lines: Line[]
  subtotal: string
  failure: string
  failure_percent: string
  unit_cost: string
  quantity: number
  total_cost: string
  markup_percent: string
  unit_price: string
  total_price: string
  profit: string
  margin_percent: string
  print_hours: string
  machine_hours_total: string
}

/** The part being costed. Strings, because these are boxes on a screen: a
 *  half-typed "1." is a state a number cannot hold. */
const BLANK = {
  grams: '',
  filament_per_kg: '',
  print_hours: '',
  print_minutes: '',
  labour_minutes: '',
  extras_each: '',
  quantity: '1',
}

const RATE_FIELDS: { key: keyof Settings; label: string; hint: string; unit: string }[] = [
  {
    key: 'machine_per_hour',
    label: 'Machine time, per hour',
    hint: "The printer wearing out: its own price over its life, plus nozzles, belts and the afternoon spent fixing it. Usually the largest line after filament, and the one shops forget.",
    unit: 'per hour',
  },
  {
    key: 'printer_watts',
    label: 'Printer draw',
    hint: 'What the machine pulls while it runs. A bed-heavy printer is nearer 300 W than 100.',
    unit: 'watts',
  },
  {
    key: 'electricity_per_kwh',
    label: 'Electricity',
    hint: 'What a unit costs you. Small per part, not small per month.',
    unit: 'per kWh',
  },
  {
    key: 'labour_per_hour',
    label: 'Labour, per hour',
    hint: 'Slicing, plate changes, supports off, sanding. Charged by the minute below.',
    unit: 'per hour',
  },
  {
    key: 'failure_percent',
    label: 'Failure allowance',
    hint: 'Some prints fail, and the ones that work have to pay for the ones that did not. Applied to everything above it — a failure wastes the machine hour as surely as the filament.',
    unit: '%',
  },
  {
    key: 'markup_percent',
    label: 'Markup',
    hint: 'What turns a cost into a price. Yours to decide; it is here so the two are read together.',
    unit: '%',
  },
]

export default function Calculator() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [part, setPart] = useState({ ...BLANK })
  const [result, setResult] = useState<Estimate | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [showRates, setShowRates] = useState(false)

  useEffect(() => {
    api
      .get<{ settings: Settings }>('/api/calculator')
      .then((data) => setSettings(data.settings))
      .catch((err) => setError(errorMessage(err)))
  }, [])

  // Recost on every keystroke, but not on every keystroke: a calculator that
  // lags behind what is typed feels broken, and one that fires a request per
  // character is rude to a NAS.
  const recost = useCallback(async () => {
    try {
      setResult(await api.post<Estimate>('/api/calculator/estimate', part))
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [part])

  useEffect(() => {
    if (!settings) return
    const timer = setTimeout(recost, 250)
    return () => clearTimeout(timer)
  }, [recost, settings])

  const saveRates = async (changes: Partial<Settings>) => {
    try {
      const data = await api.put<{ settings: Settings }>('/api/calculator', changes)
      setSettings(data.settings)
      setSaved(true)
      // The rates changed, so the figure on screen is now stale.
      await recost()
    } catch (err) {
      setError(errorMessage(err))
    }
  }

  if (!settings) {
    return (
      <div className="flex h-full items-center justify-center">
        {error ? <Alert tone="error">{error}</Alert> : <Spinner className="h-6 w-6" />}
      </div>
    )
  }

  const set = (key: keyof typeof BLANK, value: string) =>
    setPart((current) => ({ ...current, [key]: value }))

  const ratesSet = RATE_FIELDS.some((field) => String(settings[field.key] ?? '').trim())
  const priced = result && Number(result.unit_price) > 0

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-5xl space-y-4">
        <header className="flex flex-wrap items-baseline gap-2">
          <h1 className="text-lg font-semibold text-ink-900">Calculator</h1>
          <p className="text-sm text-ink-500">
            What a printed part costs to make, and what it would have to sell for.
          </p>
        </header>

        {error ? <Alert tone="error">{error}</Alert> : null}

        {!ratesSet ? (
          <Alert tone="info">
            Set your rates below and they are remembered — machine, power and
            labour are the same for everything you print, so they are typed once
            rather than every time.
          </Alert>
        ) : null}

        <div className="grid gap-4 lg:grid-cols-2">
          <Card className="space-y-3 p-4">
            <h2 className="text-sm font-semibold text-ink-800">This part</h2>

            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Filament used" hint="What the slicer says. Grams.">
                <input
                  className={inputClass}
                  inputMode="decimal"
                  placeholder="48"
                  value={part.grams}
                  onChange={(e) => set('grams', e.target.value)}
                />
              </Field>
              <Field label="Filament price" hint="Per kilogram — what the spool cost.">
                <div className="flex gap-2">
                  {/* min-w-0 or the picker beside it squeezes the field it is
                      meant to fill: a flex item will not shrink below its
                      content width without being told it may. */}
                  <input
                    className={cx(inputClass, 'min-w-0 flex-1')}
                    inputMode="decimal"
                    placeholder="22.50"
                    value={part.filament_per_kg}
                    onChange={(e) => set('filament_per_kg', e.target.value)}
                  />
                  {/* Width on the wrapper, not on the select: `inputClass`
                      carries `w-full`, and which of two width utilities wins is
                      decided by their order in Tailwind's stylesheet rather
                      than in the class attribute — so `w-28` here quietly lost
                      and the field it sits beside was squeezed to nothing. */}
                  {settings.filaments.length ? (
                    <div className="w-28 shrink-0">
                      <select
                        className={inputClass}
                        value=""
                        aria-label="Use a saved filament"
                        onChange={(e) => {
                          const row = settings.filaments.find(
                            (f) => f.name === e.target.value,
                          )
                          if (row) set('filament_per_kg', String(row.price_per_kg ?? ''))
                        }}
                      >
                        <option value="">Saved…</option>
                        {settings.filaments.map((row) => (
                          <option key={row.name} value={row.name}>
                            {row.name}
                          </option>
                        ))}
                      </select>
                    </div>
                  ) : null}
                </div>
              </Field>
            </div>

            <Field label="Print time" hint="For one of them, as the slicer estimates it.">
              <div className="flex items-center gap-2">
                <input
                  className={inputClass}
                  inputMode="numeric"
                  placeholder="4"
                  value={part.print_hours}
                  onChange={(e) => set('print_hours', e.target.value)}
                />
                <span className="text-sm text-ink-500">h</span>
                <input
                  className={inputClass}
                  inputMode="numeric"
                  placeholder="12"
                  value={part.print_minutes}
                  onChange={(e) => set('print_minutes', e.target.value)}
                />
                <span className="text-sm text-ink-500">m</span>
              </div>
            </Field>

            <div className="grid gap-3 sm:grid-cols-2">
              <Field
                label="Hands-on time"
                hint="Slicing, plate change, supports off, sanding. Minutes."
              >
                <input
                  className={inputClass}
                  inputMode="decimal"
                  placeholder="6"
                  value={part.labour_minutes}
                  onChange={(e) => set('labour_minutes', e.target.value)}
                />
              </Field>
              <Field label="Extras, each" hint="Packaging, an insert, a magnet.">
                <input
                  className={inputClass}
                  inputMode="decimal"
                  placeholder="0.40"
                  value={part.extras_each}
                  onChange={(e) => set('extras_each', e.target.value)}
                />
              </Field>
            </div>

            <Field label="How many" hint="The breakdown stays per part; the totals follow this.">
              <input
                className={cx(inputClass, 'max-w-28')}
                inputMode="numeric"
                value={part.quantity}
                onChange={(e) => set('quantity', e.target.value)}
              />
            </Field>

            <Button size="sm" variant="ghost" onClick={() => setPart({ ...BLANK })}>
              Clear
            </Button>
          </Card>

          <Card className="space-y-3 p-4">
            <h2 className="text-sm font-semibold text-ink-800">What it costs</h2>
            {result ? (
              <>
                <dl className="space-y-1 text-sm">
                  {result.lines.map((line) => (
                    <div key={line.key}>
                      <div className="flex justify-between text-ink-700">
                        <dt>{line.label}</dt>
                        <dd className="tabular-nums">{formatMoney(line.amount)}</dd>
                      </div>
                      {/* How the figure was reached, so it can be checked
                          rather than believed. */}
                      {line.detail ? (
                        <p className="pl-3 text-xs text-ink-400">{line.detail}</p>
                      ) : null}
                    </div>
                  ))}

                  <div className="flex justify-between border-t border-ink-200 pt-1 text-ink-700">
                    <dt>Subtotal</dt>
                    <dd className="tabular-nums">{formatMoney(result.subtotal)}</dd>
                  </div>
                  {Number(result.failure) > 0 ? (
                    <div className="flex justify-between text-ink-600">
                      <dt>Failure allowance ({result.failure_percent}%)</dt>
                      <dd className="tabular-nums">{formatMoney(result.failure)}</dd>
                    </div>
                  ) : null}

                  <div className="flex justify-between border-t border-ink-200 pt-1 text-base font-semibold text-ink-900">
                    <dt>Cost each</dt>
                    <dd className="tabular-nums">{formatMoney(result.unit_cost)}</dd>
                  </div>
                  {result.quantity > 1 ? (
                    <div className="flex justify-between font-medium text-ink-800">
                      <dt>Cost for {result.quantity}</dt>
                      <dd className="tabular-nums">{formatMoney(result.total_cost)}</dd>
                    </div>
                  ) : null}
                </dl>

                {priced ? (
                  <div className="rounded-md bg-ink-50 p-3">
                    <dl className="space-y-1 text-sm">
                      <div className="flex justify-between font-semibold text-ink-900">
                        <dt>Price each at {result.markup_percent}%</dt>
                        <dd className="tabular-nums">{formatMoney(result.unit_price)}</dd>
                      </div>
                      {result.quantity > 1 ? (
                        <div className="flex justify-between text-ink-700">
                          <dt>Price for {result.quantity}</dt>
                          <dd className="tabular-nums">{formatMoney(result.total_price)}</dd>
                        </div>
                      ) : null}
                      <div className="flex justify-between text-ink-600">
                        <dt>Kept, each</dt>
                        <dd className="tabular-nums">
                          {formatMoney(result.profit)} · {result.margin_percent}% margin
                        </dd>
                      </div>
                    </dl>
                    <p className="mt-1.5 text-xs text-ink-500">
                      Before Etsy takes its cut, and before postage.
                    </p>
                  </div>
                ) : null}

                {/* The other thing being spent. An eight-hour part at a good
                    margin can still be the wrong thing to put on a machine. */}
                {Number(result.print_hours) > 0 ? (
                  <p className="text-xs text-ink-500">
                    Ties up a machine for {result.machine_hours_total} hours
                    {result.quantity > 1 ? ` (${result.print_hours} each)` : ''}.
                  </p>
                ) : null}
              </>
            ) : (
              <p className="text-sm text-ink-500">Fill anything in and this fills itself in.</p>
            )}
          </Card>
        </div>

        <Card className="p-4">
          <button
            type="button"
            className="flex w-full items-center gap-2 text-left"
            onClick={() => setShowRates((v) => !v)}
          >
            <h2 className="text-sm font-semibold text-ink-800">Your rates</h2>
            <span className="text-xs text-ink-500">
              {ratesSet ? 'the same for everything you print' : 'not set yet'}
            </span>
            <span className="ml-auto text-xs text-ink-500">{showRates ? 'Hide' : 'Show'}</span>
          </button>

          {showRates ? (
            <div className="mt-3 space-y-3">
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {RATE_FIELDS.map((field) => (
                  <Field key={field.key} label={field.label} hint={field.hint}>
                    <div className="flex items-center gap-2">
                      <input
                        className={inputClass}
                        inputMode="decimal"
                        defaultValue={String(settings[field.key] ?? '')}
                        onBlur={(e) => {
                          if (e.target.value !== String(settings[field.key] ?? ''))
                            saveRates({ [field.key]: e.target.value } as Partial<Settings>)
                        }}
                      />
                      <span className="shrink-0 text-xs text-ink-500">{field.unit}</span>
                    </div>
                  </Field>
                ))}
              </div>

              <FilamentList settings={settings} onSave={saveRates} />
              {saved ? <p className="text-xs text-emerald-700">Saved.</p> : null}
            </div>
          ) : null}
        </Card>
      </div>
    </div>
  )
}

/** Spools worth remembering, so a price is picked rather than looked up. */
function FilamentList({
  settings,
  onSave,
}: {
  settings: Settings
  onSave: (changes: Partial<Settings>) => Promise<void>
}) {
  const [name, setName] = useState('')
  const [price, setPrice] = useState('')

  const rows = settings.filaments ?? []

  return (
    <div className="rounded-md bg-ink-50 p-3">
      <p className="text-sm font-medium text-ink-800">Filaments</p>
      <p className="text-xs text-ink-500">
        What a spool costs, per kilogram. Saved here so the price above is picked
        rather than remembered.
      </p>

      {rows.length ? (
        <ul className="mt-2 space-y-1">
          {rows.map((row, index) => (
            <li key={index} className="flex flex-wrap items-center gap-2 text-xs">
              <span className="min-w-24 font-medium text-ink-800">{row.name}</span>
              <input
                className={cx(inputClass, 'max-w-28')}
                inputMode="decimal"
                placeholder="per kg"
                defaultValue={String(row.price_per_kg ?? '')}
                onBlur={(e) => {
                  if (e.target.value === String(row.price_per_kg ?? '')) return
                  onSave({
                    filaments: rows.map((r, i) =>
                      i === index ? { ...r, price_per_kg: e.target.value } : r,
                    ),
                  })
                }}
              />
              <Button
                size="sm"
                variant="ghost"
                onClick={() => onSave({ filaments: rows.filter((_, i) => i !== index) })}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      ) : null}

      <div className="mt-2 flex flex-wrap items-end gap-2">
        <Field label="Name">
          <input
            className={cx(inputClass, 'max-w-40')}
            placeholder="PLA Matte"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <Field label="Per kg">
          <input
            className={cx(inputClass, 'max-w-28')}
            inputMode="decimal"
            placeholder="22.50"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
          />
        </Field>
        <Button
          size="sm"
          disabled={!name.trim()}
          onClick={async () => {
            await onSave({
              filaments: [...rows, { name: name.trim(), price_per_kg: price }],
            })
            setName('')
            setPrice('')
          }}
        >
          Add
        </Button>
      </div>
    </div>
  )
}
