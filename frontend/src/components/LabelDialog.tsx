import { useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type { Order } from '../lib/types'
import { formatMoney } from '../lib/format'

/** What buying a label gives back. The cost is the reason this is shown at all:
 *  it is the only action in PrintFlow that spends money. */
interface Bought {
  tracking_number: string
  carrier_code: string | null
  service_code: string | null
  label_cost: string | null
  label_currency: string | null
}
import { Alert, Button, Field, Modal, Spinner, inputClass } from './ui'

/** What a carrier would charge per service. A quote, not the price: the carrier
 *  prices again when the label is actually bought. */
interface Quote {
  available: boolean
  reason?: string
  from_postal_code?: string
  to_postal_code?: string
  ship_from?: ShipFrom | null
  rates?: { service_code: string; service_name: string | null; total: string | null }[]
}

/** One ship-from location, as a person picks it: the name they call it,
 *  and enough address to tell two of them apart. A warehouse id on its own
 *  is a number nobody recognises, and picking the wrong one means a carrier
 *  collecting from the wrong building. */
interface ShipFrom {
  warehouse_id: number | string
  name: string | null
  address: string | null
  label: string
  postal_code: string | null
  is_default: boolean
}

interface LabelContext {
  available: boolean
  reason?: string
  defaults?: {
    carrier_code: string | null
    service_code: string | null
    package_code: string | null
    confirmation: string | null
    weight_value: number | null
    weight_units: string | null
    ship_to: Record<string, any>
  }
  carriers?: { code: string; name: string }[]
  /** Where it would ship from as things stand, and the alternatives. */
  ship_from?: ShipFrom | null
  ship_from_options?: ShipFrom[]
}

export default function LabelDialog({
  order,
  onClose,
  onCreated,
}: {
  order: Order
  onClose: () => void
  onCreated: () => void
}) {
  const [context, setContext] = useState<LabelContext | null>(null)
  const [services, setServices] = useState<{ code: string; name: string }[]>([])
  const [packages, setPackages] = useState<{ code: string; name: string }[]>([])
  const [carrier, setCarrier] = useState('')
  const [service, setService] = useState('')
  const [packageCode, setPackageCode] = useState('package')
  const [weight, setWeight] = useState('')
  const [units, setUnits] = useState('ounces')
  // Empty means "whatever the shop default and the order resolve to", which is
  // what every label bought before this feature used.
  const [warehouse, setWarehouse] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [bought, setBought] = useState<Bought | null>(null)
  const [quote, setQuote] = useState<Quote | null>(null)
  const [quoting, setQuoting] = useState(false)

  // What the carrier would charge, asked whenever the answer would change.
  // Debounced because the weight box is typed into a digit at a time, and
  // every keystroke would otherwise be a request to ShipStation.
  useEffect(() => {
    const grams = Number(weight)
    if (!carrier || !(grams > 0)) {
      setQuote(null)
      return
    }
    setQuoting(true)
    const timer = window.setTimeout(() => {
      const query = new URLSearchParams({
        carrier_code: carrier,
        weight_value: String(grams),
        weight_units: units,
        package_code: packageCode || '',
        ...(warehouse ? { warehouse_id: warehouse } : {}),
      })
      api
        .get<Quote>(`/api/orders/${order.id}/label-rates?${query}`)
        .then(setQuote)
        // A quote that will not come is not an error on this screen: the label
        // can still be bought, just without knowing the price first.
        .catch(() => setQuote({ available: false, reason: 'ShipStation would not quote.' }))
        .finally(() => setQuoting(false))
    }, 500)
    return () => {
      window.clearTimeout(timer)
      setQuoting(false)
    }
  }, [order.id, carrier, weight, units, packageCode, warehouse])

  useEffect(() => {
    api
      .get<LabelContext>(`/api/orders/${order.id}/label-context`)
      .then((data) => {
        setContext(data)
        const defaults = data.defaults
        if (defaults) {
          setCarrier(defaults.carrier_code ?? '')
          setService(defaults.service_code ?? '')
          setPackageCode(defaults.package_code ?? 'package')
          setWeight(defaults.weight_value ? String(defaults.weight_value) : '')
          setUnits(defaults.weight_units ?? 'ounces')
        }
      })
      .catch((err) => setError(errorMessage(err)))
  }, [order.id])

  // Services and package types depend on the chosen carrier.
  useEffect(() => {
    if (!carrier) return
    Promise.all([
      api.get<{ services: any[] }>(`/api/integrations/shipstation/services?carrier=${carrier}`),
      api.get<{ packages: any[] }>(`/api/integrations/shipstation/packages?carrier=${carrier}`),
    ])
      .then(([s, p]) => {
        setServices(s.services.map((x) => ({ code: x.code, name: x.name })))
        setPackages(p.packages.map((x) => ({ code: x.code, name: x.name })))
      })
      .catch(() => {
        setServices([])
        setPackages([])
      })
  }, [carrier])

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const receipt = await api.post<Bought>(`/api/orders/${order.id}/label`, {
        carrier_code: carrier,
        service_code: service,
        package_code: packageCode || 'package',
        weight_value: Number(weight),
        weight_units: units,
        ...(warehouse ? { warehouse_id: warehouse } : {}),
        allow_not_ready: order.status !== 'ready_to_ship',
      })
      // The dialog stays up to say what it cost. This is the one action in
      // PrintFlow that spends money, and closing on success would hide the
      // number at the only moment somebody is thinking about it.
      setBought(receipt)
      onCreated()
    } catch (err) {
      setError(errorMessage(err))
      setConfirming(false)
    } finally {
      setBusy(false)
    }
  }

  const rateFor = (code: string) =>
    (quote?.rates ?? []).find((rate) => rate.service_code === code)?.total ?? null
  const chosen = service ? rateFor(service) : null

  const ready = carrier && service && Number(weight) > 0

  return (
    <Modal open title={`Create label — order #${order.order_number}`} onClose={onClose}>
      {!context ? (
        <div className="flex justify-center py-6">
          <Spinner className="h-6 w-6" />
        </div>
      ) : bought ? (
        <div className="space-y-3">
          <Alert tone="success">
            Label bought{' '}
            {bought.label_cost
              ? `for ${formatMoney(bought.label_cost, bought.label_currency)}`
              : '— ShipStation did not price it'}
            .
          </Alert>
          <p className="text-sm text-ink-700">
            Tracking <span className="font-mono">{bought.tracking_number}</span>
          </p>
          <p className="text-xs text-ink-500">
            {bought.carrier_code} / {bought.service_code}. ShipStation pushes the
            tracking number back to Etsy.
          </p>
          <div className="flex justify-end">
            <Button variant="primary" onClick={onClose}>
              Done
            </Button>
          </div>
        </div>
      ) : !context.available ? (
        <Alert tone="warning">{context.reason}</Alert>
      ) : (
        <div className="space-y-4">
          <Alert tone="info">
            This buys a real label from ShipStation and costs money. ShipStation sends the
            tracking number back to Etsy on its own.
          </Alert>

          {context.defaults?.ship_to?.name ? (
            <div className="rounded-md bg-ink-50 p-3 text-sm text-ink-700">
              <p className="font-medium">{context.defaults.ship_to.name}</p>
              <p className="text-xs text-ink-500">
                {[
                  context.defaults.ship_to.street1,
                  context.defaults.ship_to.city,
                  context.defaults.ship_to.state,
                  context.defaults.ship_to.postalCode,
                  context.defaults.ship_to.country,
                ]
                  .filter(Boolean)
                  .join(', ')}
              </p>
            </div>
          ) : null}

          {/* Where it leaves from, above the carrier because that is the
              order the questions are actually asked in: which building, then
              who collects, then how fast. Shown even with one location, so a
              label's origin is never something you have to already know. */}
          {(context.ship_from_options ?? []).length ? (
            <Field
              label="Ship from"
              hint={
                warehouse
                  ? 'This label only. The shop default is unchanged.'
                  : context.ship_from
                    ? 'The shop default, set under Settings → ShipStation.'
                    : undefined
              }
            >
              <select
                className={inputClass}
                value={warehouse || String(context.ship_from?.warehouse_id ?? '')}
                onChange={(e) => setWarehouse(e.target.value)}
              >
                {(context.ship_from_options ?? []).map((row) => (
                  <option key={row.warehouse_id} value={String(row.warehouse_id)}>
                    {row.label}
                  </option>
                ))}
              </select>
            </Field>
          ) : context.available ? (
            <Alert tone="warning">
              ShipStation has no ship-from address, so nothing can be priced or
              posted. Add a warehouse origin in ShipStation, then pick it under
              Settings → ShipStation.
            </Alert>
          ) : null}

          <Field label="Carrier">
            <select
              className={inputClass}
              value={carrier}
              onChange={(e) => {
                setCarrier(e.target.value)
                setService('')
              }}
            >
              <option value="">Select a carrier…</option>
              {(context.carriers ?? []).map((c) => (
                <option key={c.code} value={c.code}>
                  {c.name}
                </option>
              ))}
            </select>
          </Field>

          <Field label="Service">
            <select
              className={inputClass}
              value={service}
              onChange={(e) => setService(e.target.value)}
            >
              <option value="">Select a service…</option>
              {services.map((s) => {
                const priced = rateFor(s.code)
                return (
                  <option key={s.code} value={s.code}>
                    {s.name}
                    {priced ? ` — ${formatMoney(priced)}` : ''}
                  </option>
                )
              })}
              {service && !services.some((s) => s.code === service) ? (
                <option value={service}>{service} (ShipStation default)</option>
              ) : null}
            </select>
          </Field>

          <Field label="Package">
            <select
              className={inputClass}
              value={packageCode}
              onChange={(e) => setPackageCode(e.target.value)}
            >
              <option value="package">Package</option>
              {packages.map((p) => (
                <option key={p.code} value={p.code}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Weight">
              <input
                className={inputClass}
                type="number"
                min="0"
                step="0.1"
                value={weight}
                onChange={(e) => setWeight(e.target.value)}
              />
            </Field>
            <Field label="Units">
              <select
                className={inputClass}
                value={units}
                onChange={(e) => setUnits(e.target.value)}
              >
                <option value="ounces">ounces</option>
                <option value="pounds">pounds</option>
                <option value="grams">grams</option>
              </select>
            </Field>
          </div>

          <QuotedPrice
            quote={quote}
            quoting={quoting}
            service={service}
            hasWeight={Number(weight) > 0}
          />

          {order.status !== 'ready_to_ship' ? (
            <Alert tone="warning">
              This order is not Ready to Ship yet. Creating a label anyway will be recorded
              in the audit log.
            </Alert>
          ) : null}
          {error ? <Alert tone="error">{error}</Alert> : null}

          <div className="flex justify-end gap-2">
            <Button onClick={onClose}>Cancel</Button>
            {confirming ? (
              <Button variant="danger" onClick={submit} disabled={busy}>
                {busy
                  ? 'Buying…'
                  : chosen
                    ? `Yes — buy it for about ${formatMoney(chosen)}`
                    : 'Yes — buy the label'}
              </Button>
            ) : (
              <Button
                variant="primary"
                onClick={() => setConfirming(true)}
                disabled={!ready}
              >
                Create label
              </Button>
            )}
          </div>
        </div>
      )}
    </Modal>
  )
}

/** The quoted price for the chosen service, said to be a quote.
 *
 *  The carrier prices again at the moment the label is bought, and a surcharge
 *  that depends on something ShipStation does not know yet lands on the real
 *  charge and not on this one. So this says "about" — a number that is nearly
 *  always right is worth a great deal when the question is which service to
 *  pick, but presenting it as the price would be a promise nobody here can
 *  make. What was actually charged appears the moment the label is bought. */
function QuotedPrice({
  quote,
  quoting,
  service,
  hasWeight,
}: {
  quote: Quote | null
  quoting: boolean
  service: string
  hasWeight: boolean
}) {
  if (!hasWeight) {
    return (
      <p className="text-xs text-ink-500">
        Enter a weight to see what each service costs.
      </p>
    )
  }
  if (quoting) {
    return <p className="text-xs text-ink-500">Asking ShipStation what this costs…</p>
  }
  if (!quote) return null
  if (!quote.available) {
    return (
      <p className="text-xs text-ink-500">
        {quote.reason ?? 'ShipStation could not price this.'} You can still buy the
        label; the price will be shown once it is bought.
      </p>
    )
  }

  const rate = (quote.rates ?? []).find((row) => row.service_code === service)
  if (!service) {
    return (
      <p className="text-xs text-ink-500">
        {(quote.rates ?? []).length} service
        {(quote.rates ?? []).length === 1 ? '' : 's'} priced — pick one to see it.
      </p>
    )
  }
  if (!rate?.total) {
    return (
      <p className="text-xs text-ink-500">
        ShipStation did not price this service. You can still buy the label.
      </p>
    )
  }
  return (
    <div className="rounded-md bg-ink-50 p-3">
      <p className="text-sm text-ink-800">
        About <span className="font-semibold">{formatMoney(rate.total)}</span> —{' '}
        {rate.service_name ?? service}
      </p>
      <p className="mt-0.5 text-xs text-ink-500">
        A quote from ShipStation for {quote.from_postal_code} → {quote.to_postal_code}.
        The carrier prices again when the label is bought, so surcharges can move
        it a little.
      </p>
    </div>
  )
}
