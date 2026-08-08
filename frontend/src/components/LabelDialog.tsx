import { useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import type { Order } from '../lib/types'
import { Alert, Button, Field, Modal, Spinner, inputClass } from './ui'

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
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirming, setConfirming] = useState(false)

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
      await api.post(`/api/orders/${order.id}/label`, {
        carrier_code: carrier,
        service_code: service,
        package_code: packageCode || 'package',
        weight_value: Number(weight),
        weight_units: units,
        allow_not_ready: order.status !== 'ready_to_ship',
      })
      onCreated()
    } catch (err) {
      setError(errorMessage(err))
      setConfirming(false)
    } finally {
      setBusy(false)
    }
  }

  const ready = carrier && service && Number(weight) > 0

  return (
    <Modal open title={`Create label — order #${order.order_number}`} onClose={onClose}>
      {!context ? (
        <div className="flex justify-center py-6">
          <Spinner className="h-6 w-6" />
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
              {services.map((s) => (
                <option key={s.code} value={s.code}>
                  {s.name}
                </option>
              ))}
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
                {busy ? 'Buying…' : 'Yes — buy the label'}
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
