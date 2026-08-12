import { useCallback, useEffect, useState } from 'react'
import { api, errorMessage } from '../lib/api'
import { JOB_STATUS_CLASSES, formatDateTime } from '../lib/format'
import type {
  CameraReport,
  FarmOverview,
  FarmPrinter,
  FarmSummary,
  FilamentSpool,
  QueueJob,
} from '../lib/types'
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
import PrintFilePicker, { type FileChoice } from '../components/PrintFilePicker'
import RawReplies from '../components/RawReplies'

/** How often the farm is re-read. A print takes hours; this is about a person
 *  watching a machine, not about the plate finishing sooner. */
const REFRESH_MS = 15_000

const FINISHED = ['done', 'cancelled']

/** What the machine is doing, in one word, whichever field the build filled in.
 *
 *  Bambu's own vocabulary (RUNNING, PAUSE, FINISH, IDLE) and Bambuddy's own
 *  (printing, idle) both land here, and an instance that reports neither still
 *  has an honest answer: offline, or nothing said. */
function machineState(printer: FarmPrinter): { label: string; tone: string } {
  if (printer.online === false) return { label: 'offline', tone: 'bg-ink-200 text-ink-600 ring-ink-300' }
  const raw = String(printer.state ?? printer.status ?? '').trim().toLowerCase()
  if (!raw) return { label: 'no status', tone: 'bg-ink-100 text-ink-500 ring-ink-300' }
  if (['running', 'printing', 'busy', 'prepare'].includes(raw))
    return { label: 'printing', tone: 'bg-amber-100 text-amber-900 ring-amber-300' }
  if (['pause', 'paused'].includes(raw))
    return { label: 'paused', tone: 'bg-orange-100 text-orange-900 ring-orange-300' }
  if (['failed', 'error'].includes(raw))
    return { label: raw, tone: 'bg-red-100 text-red-800 ring-red-300' }
  if (['idle', 'finish', 'finished', 'ready'].includes(raw))
    return { label: raw === 'finish' ? 'finished' : raw, tone: 'bg-emerald-100 text-emerald-800 ring-emerald-300' }
  return { label: raw, tone: 'bg-sky-100 text-sky-800 ring-sky-300' }
}

function temperature(now: number | null, target: number | null): string | null {
  if (now === null) return null
  const reading = `${Math.round(now)}°`
  // A target of zero means it is not heating at all, which is what "idle"
  // already said. Only a target it is working towards is worth the space.
  return target ? `${reading}/${Math.round(target)}°` : reading
}

/** Whether a spool is close enough to the end to lose a plate on.
 *
 *  Not a warning and not a threshold anyone tuned — a tenth of a reel is
 *  roughly where a long print stops being a safe thing to start, and a shop
 *  that wants to ignore it can. A machine that reports no level at all is not
 *  low; it is silent, which is a different thing and must not be coloured. */
function lowSpool(remaining: number | null): boolean {
  return remaining !== null && remaining <= 10
}

/** How a spool reads in one line: "PLA Matte black 82%".
 *
 *  Everything the build actually said and nothing invented — a machine that
 *  reports a type and no colour says the type, and one that reports neither
 *  is not described as an unknown spool, because it may be an empty slot. */
function spoolText(spool: FilamentSpool): string {
  return [
    spool.brand || spool.type,
    spool.colour,
    spool.remaining === null ? null : `${spool.remaining}%`,
  ]
    .filter(Boolean)
    .join(' ')
}

/** The colour as a square, where the machine gave one that can be drawn.
 *
 *  A ring rather than a bare block, because white and natural filament are
 *  real and would otherwise be an invisible square on a white card. */
function Swatch({ spool, size = 'sm' }: { spool: FilamentSpool; size?: 'sm' | 'md' }) {
  if (!spool.colour_hex) return null
  return (
    <span
      aria-hidden
      className={cx(
        'inline-block shrink-0 rounded-full ring-1 ring-inset ring-ink-900/20',
        size === 'md' ? 'h-3.5 w-3.5' : 'h-2.5 w-2.5',
      )}
      style={{ backgroundColor: spool.colour_hex }}
    />
  )
}

function duration(minutes: number | null): string | null {
  if (minutes === null || minutes <= 0) return null
  if (minutes < 60) return `${minutes} min left`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours}h ${rest}m left` : `${hours}h left`
}

export default function Printers() {
  const [farm, setFarm] = useState<FarmOverview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [showFinished, setShowFinished] = useState(false)
  // Every farm read is also when the cards want a new picture.
  const [tick, setTick] = useState(0)
  const [watching, setWatching] = useState<string | null>(null)
  // Machines whose camera was offered and did not answer. A farm where none of
  // them answered is a farm with a question to ask, not ten broken cards.
  const [blind, setBlind] = useState<string[]>([])

  const load = useCallback(async () => {
    try {
      setFarm(await api.get<FarmOverview>('/api/printers'))
      setTick((n) => n + 1)
      setError(null)
    } catch (err) {
      setError(errorMessage(err))
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  const act = async (jobId: string, action: 'requeue' | 'cancel') => {
    setBusy(jobId)
    try {
      await api.post(`/api/print-jobs/${jobId}/${action}`)
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const dispatchAll = async () => {
    setBusy('dispatch')
    try {
      await api.post('/api/print-jobs/dispatch')
      await load()
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(null)
    }
  }

  const plate = (job: QueueJob) => (
    <Plate key={job.id} job={job} busy={busy === job.id} onAct={act} />
  )
  // On a machine card the time a plate was created says nothing the machine's
  // own progress does not; in a list with no machine to look at, it does.
  const cardPlate = (job: QueueJob) => (
    <Plate key={job.id} job={job} busy={busy === job.id} onAct={act} compact />
  )

  const watched =
    (farm?.printers ?? []).find((row) => String(row.id) === watching) ?? null
  // Whether any machine is actually showing one.
  const showing = (farm?.printers ?? []).some(
    (row) => row.camera && !blind.includes(String(row.id)),
  )
  const pending = (farm?.plates ?? []).filter((job) => job.status === 'pending')
  const finished = (farm?.plates ?? []).filter((job) => FINISHED.includes(job.status))

  return (
    <div className="h-full overflow-y-auto p-3 sm:p-6">
      <div className="mx-auto max-w-6xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-lg font-semibold text-ink-900">Printers</h1>
          {farm ? (
            <span className="text-xs text-ink-500">
              {farm.printers.length} machine{farm.printers.length === 1 ? '' : 's'}
              {farm.printers.filter((p) => p.online !== false).length < farm.printers.length
                ? ` · ${farm.printers.filter((p) => p.online === false).length} offline`
                : ''}
            </span>
          ) : null}
          <div className="ml-auto flex gap-1.5">
            <Button size="sm" variant="ghost" onClick={load}>
              Refresh
            </Button>
            {pending.length ? (
              <Button variant="primary" onClick={dispatchAll} disabled={busy === 'dispatch'}>
                {busy === 'dispatch'
                  ? 'Sending…'
                  : `Send ${pending.length} waiting plate${pending.length === 1 ? '' : 's'}`}
              </Button>
            ) : null}
          </div>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}
        {farm?.error ? (
          <Alert tone="warning">
            The farm could not be read — {farm.error} The plates below are PrintFlow's
            own record and are still correct; only what each machine is doing is missing.
          </Alert>
        ) : null}
        {farm?.detail_error ? (
          <Alert tone="warning">
            One machine would not answer: {farm.detail_error}
          </Alert>
        ) : null}

        {!farm ? (
          <div className="flex justify-center py-10">
            <Spinner className="h-6 w-6" />
          </div>
        ) : (
          <>
            {farm.summary && farm.printers.length ? (
              <FarmSummaryStrip summary={farm.summary} />
            ) : null}

            {farm.printers.length === 0 ? (
              <EmptyState
                title={farm.error ? 'The farm could not be read' : 'Bambuddy listed no printers'}
                description={
                  farm.error
                    ? 'Check the connection under Settings → Bambuddy.'
                    : 'Every machine Bambuddy knows about appears here. If yours are missing, the printers endpoint may be pointed at the wrong path — Settings → Bambuddy → Advanced.'
                }
              />
            ) : (
              <div className="grid items-start gap-3 md:grid-cols-2 xl:grid-cols-3">
                {farm.printers.map((printer) => (
                  <PrinterCard
                    key={String(printer.id ?? printer.name)}
                    printer={printer}
                    renderPlate={cardPlate}
                    tick={tick}
                    onWatch={() => setWatching(String(printer.id))}
                    onBlind={() =>
                      setBlind((seen) =>
                        seen.includes(String(printer.id))
                          ? seen
                          : [...seen, String(printer.id)],
                      )
                    }
                  />
                ))}
              </div>
            )}

            {farm.printers.length > 0 && !farm.error && !showing ? <WhyNoCameras /> : null}

            {!farm.live && farm.printers.length > 0 && !farm.error ? (
              <Card className="space-y-2 p-3">
                <p className="text-sm text-ink-700">
                  Bambuddy listed the machines but said nothing about what any of them
                  is doing — no state, no progress, no temperatures.
                </p>
                <p className="text-xs text-ink-500">
                  Some builds keep the live readings on a per-printer endpoint instead
                  of the farm listing. PrintFlow asks for that one too and it answered
                  with nothing either, so the readings are somewhere it has not been
                  pointed at. The replies below say where they are not.
                </p>
                <RawReplies endpoint="/api/printers/raw" />
              </Card>
            ) : null}

            <UnplacedPlates plates={farm.unplaced} renderPlate={plate} />

            {finished.length ? (
              <Card className="p-3">
                <button
                  type="button"
                  className="text-sm font-medium text-ink-700"
                  onClick={() => setShowFinished((open) => !open)}
                >
                  {showFinished ? 'Hide' : 'Show'} {finished.length} finished plate
                  {finished.length === 1 ? '' : 's'}
                </button>
                {showFinished ? (
                  <div className="mt-2 divide-y divide-ink-200">
                    {finished.map(plate)}
                  </div>
                ) : null}
              </Card>
            ) : null}
          </>
        )}
      </div>

      {watched ? (
        <PrinterDetail
          printer={watched}
          onClose={() => setWatching(null)}
          onPrinted={load}
        />
      ) : null}
    </div>
  )
}

function PrinterCard({
  printer,
  renderPlate,
  tick,
  onWatch,
  onBlind,
}: {
  printer: FarmPrinter
  renderPlate: (job: QueueJob) => JSX.Element
  /** Bumped when the farm is re-read, which is when a new frame is wanted. */
  tick: number
  onWatch: () => void
  onBlind: () => void
}) {
  const state = machineState(printer)
  const nozzle = temperature(printer.nozzle_temp, printer.nozzle_target)
  const bed = temperature(printer.bed_temp, printer.bed_target)
  const chamber = temperature(printer.chamber_temp, null)
  const left = duration(printer.remaining_minutes)

  return (
    <Card className={cx('flex flex-col gap-2 p-3', printer.online === false && 'opacity-70')}>
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={cx(
            'h-2 w-2 shrink-0 rounded-full',
            printer.online === false ? 'bg-ink-300' : 'bg-emerald-500',
          )}
          title={printer.online === false ? 'Offline' : 'Online'}
        />
        {/* A machine with no id cannot be asked anything further — no page to
            open, no camera to fetch, nothing to print on. It is still a
            machine and still gets a card. */}
        {printer.id === null ? (
          <span className="min-w-0 flex-1 truncate text-sm font-semibold text-ink-900">
            {printer.name ?? 'Unnamed printer'}
          </span>
        ) : (
          <button
            type="button"
            onClick={onWatch}
            className="min-w-0 flex-1 truncate text-left text-sm font-semibold text-ink-900 underline decoration-ink-300 underline-offset-2 hover:decoration-ink-800"
            title="Everything this machine reports"
          >
            {printer.name ?? `Printer ${printer.id}`}
          </button>
        )}
        {printer.model ? <Badge>{printer.model}</Badge> : null}
        <Badge className={state.tone}>{state.label}</Badge>
      </div>

      <CameraView printer={printer} tick={tick} onWatch={onWatch} onBlind={onBlind} />

      {/* An idle machine reports 0% because there is nothing on it, and a bar
          at zero reads as a print that has not started rather than as no print
          at all. So the bar belongs to a machine that is actually working. */}
      {printer.progress !== null && (printer.progress > 0 || state.label === 'printing') ? (
        <div>
          <div className="h-1.5 overflow-hidden rounded-full bg-ink-200">
            <div
              className="h-full rounded-full bg-sky-500"
              style={{ width: `${Math.min(100, Math.max(0, printer.progress))}%` }}
            />
          </div>
          <p className="mt-1 text-xs text-ink-500">
            {printer.progress}%
            {left ? ` · ${left}` : ''}
            {printer.layers ? ` · layer ${printer.layer ?? '—'}/${printer.layers}` : ''}
          </p>
        </div>
      ) : null}

      {printer.current_file ? (
        <p className="truncate text-xs text-ink-600" title={printer.current_file}>
          {printer.current_file}
        </p>
      ) : null}

      {nozzle || bed || chamber ? (
        <p className="flex flex-wrap gap-x-3 text-xs text-ink-500">
          {nozzle ? <span>nozzle {nozzle}</span> : null}
          {bed ? <span>bed {bed}</span> : null}
          {chamber ? <span>chamber {chamber}</span> : null}
          {printer.nozzle_diameter ? <span>{printer.nozzle_diameter}mm</span> : null}
        </p>
      ) : null}

      {/* What is loaded, at a glance. The one thing that decides whether a
          plate can go on this machine at all, and the one thing a card of
          temperatures never tells you. */}
      {printer.filament?.length ? (
        <p className="flex flex-wrap gap-1 text-xs">
          {printer.filament.slice(0, 4).map((spool) => (
            <span
              key={String(spool.slot)}
              className={cx(
                'inline-flex items-center gap-1 rounded px-1.5 py-0.5',
                lowSpool(spool.remaining)
                  ? 'bg-amber-100 text-amber-900'
                  : 'bg-ink-100 text-ink-600',
              )}
              title={`Slot ${spool.slot} · ${spoolText(spool) || 'nothing reported'}${
                lowSpool(spool.remaining) ? ' — nearly empty' : ''
              }`}
            >
              <Swatch spool={spool} />
              {/* The colour word only when there is no square to show it: four
                  chips of "PLA black 82%" is a card nobody reads. */}
              {spool.type ?? spool.colour ?? '—'}
              {spool.remaining === null ? '' : ` ${spool.remaining}%`}
            </span>
          ))}
          {printer.filament.length > 4 ? (
            <span className="text-ink-400">+{printer.filament.length - 4}</span>
          ) : null}
        </p>
      ) : null}

      {printer.error ? (
        <p className="text-xs text-red-700">{String(printer.error)}</p>
      ) : null}

      <div className="mt-auto border-t border-ink-200 pt-2">
        {printer.plates.length ? (
          <div className="divide-y divide-ink-100">{printer.plates.map(renderPlate)}</div>
        ) : (
          <p className="text-xs text-ink-400">No plates from PrintFlow on this machine.</p>
        )}
      </div>
    </Card>
  )
}

/** The picture on a card: one frame, replaced whenever the farm is re-read.
 *
 *  A frame rather than a live stream. Every camera PrintFlow can reach is
 *  behind Bambuddy's API key and on the shop LAN, so the picture is proxied;
 *  proxying a live stream would mean one socket per card held open for as long
 *  as the tab is, which is a lot to spend on a card the size of a stamp. The
 *  enlarged view, where somebody is actually watching, refreshes every second.
 *
 *  A camera that fails is not an error worth a banner — a machine may simply
 *  not have one. It takes the space back and says so, once. */
function CameraView({
  printer,
  tick,
  onWatch,
  onBlind,
}: {
  printer: FarmPrinter
  tick: number
  onWatch: () => void
  onBlind: () => void
}) {
  const [broken, setBroken] = useState(false)

  useEffect(() => setBroken(false), [printer.id])

  if (!printer.camera) {
    // A camera the build named but PrintFlow will not fetch: it points
    // somewhere other than Bambuddy, and a browser on that network may still
    // reach it even though the server here should not go looking.
    return printer.camera_url ? (
      <a
        className="text-xs text-sky-700 underline"
        href={printer.camera_url}
        target="_blank"
        rel="noreferrer"
      >
        Camera (opens on your network)
      </a>
    ) : null
  }

  if (broken) {
    return <p className="text-xs text-ink-400">No camera on this machine.</p>
  }

  return (
    <button
      type="button"
      onClick={onWatch}
      className="block overflow-hidden rounded-md bg-ink-900"
      title="Watch this machine"
    >
      <img
        src={`/api/printers/${printer.id}/camera?t=${tick}`}
        alt={`Camera on ${printer.name ?? 'this printer'}`}
        className="h-32 w-full object-cover"
        onError={() => {
          setBroken(true)
          onBlind()
        }}
      />
    </button>
  )
}

/** Why the farm has no pictures on it — asked of the instance, not guessed.
 *
 *  Four causes look identical from a card with nothing on it: the build has no
 *  camera, it has one under a name PrintFlow does not recognise, it has one
 *  that is not in its OpenAPI document at all, or it has one that answers with
 *  something that is not a picture. Each has a different fix, and only the
 *  instance can tell them apart. */
function WhyNoCameras() {
  const [report, setReport] = useState<CameraReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const ask = (again: boolean) => {
    setBusy(true)
    api
      .get<CameraReport>(`/api/printers/cameras${again ? '?again=true' : ''}`)
      .then((data) => {
        setReport(data)
        setError(null)
      })
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setBusy(false))
  }

  return (
    <Card className="space-y-2 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold text-ink-800">No cameras</h2>
        <p className="min-w-48 flex-1 text-xs text-ink-500">
          Nothing on these cards is showing a picture.
        </p>
        <Button size="sm" variant="ghost" onClick={() => ask(false)} disabled={busy}>
          {busy ? 'Asking…' : 'Why?'}
        </Button>
        {report ? (
          <Button size="sm" onClick={() => ask(true)} disabled={busy}>
            Look again
          </Button>
        ) : null}
      </div>

      {error ? <Alert tone="error">{error}</Alert> : null}

      {report ? (
        <div className="space-y-2 text-xs text-ink-600">
          <p>
            {report.candidates.length === 0
              ? `This Bambuddy's own document (${report.spec_path ?? 'unknown'}) mentions no
                 camera endpoint at all. Either the build has none, or it serves one
                 that is not in the document — in which case naming the path under
                 Settings → Bambuddy → Advanced as "One printer's camera" is the fix,
                 then press Look again.`
              : `The instance serves the endpoints below. If one of them is the camera
                 and PrintFlow picked the wrong one, put it under Settings → Bambuddy →
                 Advanced as "One printer's camera", with {printer_id} where the machine
                 goes, then press Look again.`}
          </p>
          <p>
            Currently calling <span className="font-mono">{report.path}</span> —{' '}
            {report.source}.
          </p>

          {report.candidates.length ? (
            <ul className="space-y-0.5 font-mono">
              {report.candidates.map((row) => (
                <li key={row.path}>
                  {row.path}{' '}
                  <span className="text-ink-400">{row.methods.join(', ')}</span>
                </li>
              ))}
            </ul>
          ) : null}

          {report.probe ? (
            <div className="space-y-1">
              <p>
                Asked <span className="font-mono">{report.probe.endpoint}</span>
                {report.probe.printer ? ` (${report.probe.printer})` : ''}:
              </p>
              <pre className="max-h-48 overflow-auto rounded-md bg-ink-900 p-2 text-[11px] leading-snug text-ink-100">
                {JSON.stringify(report.probe, null, 2)}
              </pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </Card>
  )
}

/** The farm in one line, above the cards.
 *
 *  Standing in the shop the first question is not about any one machine: it is
 *  how much of the farm is working, how much of it is standing idle, and when
 *  the busy ones come free. That is three numbers and a time, and reading them
 *  off ten cards is work the page can do instead. */
function FarmSummaryStrip({ summary }: { summary: FarmSummary }) {
  const free = duration(summary.busy_until_minutes)
  // Every state the farm reported that is not one of the three named below —
  // paused, failed, and whatever a build calls something PrintFlow has no word
  // for. These are the ones worth walking over to, so they are not swallowed.
  const others = Object.entries(summary.by_state)
    .filter(([name]) => !['printing', 'idle', 'offline'].includes(name))
    .sort((a, b) => b[1] - a[1])

  return (
    <Card className="flex flex-wrap items-center gap-x-6 gap-y-2 p-3">
      <Stat label="Machines" value={summary.machines} />
      <Stat label="Printing" value={summary.printing} tone="text-amber-700" />
      <Stat label="Idle" value={summary.idle} tone="text-emerald-700" />
      {summary.offline ? (
        <Stat label="Offline" value={summary.offline} tone="text-ink-500" />
      ) : null}
      {others.map(([name, count]) => (
        <Stat key={name} label={name} value={count} tone="text-sky-700" />
      ))}
      <div className="hidden h-8 w-px bg-ink-200 sm:block" />
      <Stat
        label="Plates open"
        value={summary.plates_open}
        note={summary.plates_waiting ? `${summary.plates_waiting} not on a machine` : null}
      />
      <Stat label="Units to make" value={summary.units_open} />
      {summary.machines && !summary.printing && !summary.plates_open ? (
        <p className="text-xs text-ink-500">Nothing outstanding.</p>
      ) : null}
      {free ? (
        <div className="ml-auto text-xs text-ink-500">
          Farm clear in {free.replace(' left', '')}
        </div>
      ) : null}
    </Card>
  )
}

function Stat({
  label,
  value,
  tone,
  note,
}: {
  label: string
  value: number
  tone?: string
  note?: string | null
}) {
  return (
    <div>
      <p className={cx('text-lg font-semibold leading-none', tone ?? 'text-ink-900')}>
        {value}
      </p>
      {/* Only the first letter: the named stats are written properly already,
          and the ones from the farm's own vocabulary arrive lower-case. */}
      <p className="mt-1 text-xs text-ink-500 first-letter:uppercase">{label}</p>
      {note ? <p className="text-xs text-amber-700">{note}</p> : null}
    </div>
  )
}

/** One machine's own page: everything it reports, and a way to print on it.
 *
 *  Three tabs because the three questions are different. *Now* is what a person
 *  standing at the machine wants — the picture, the progress, the plates on it.
 *  *Machine* is what it is rather than what it is doing, which barely changes
 *  and is what you need when something is wrong. *Everything* is the honest
 *  answer to "all available metrics": PrintFlow cannot know what a given build
 *  reports, so the whole flattened reply is there, untouched. */
function PrinterDetail({
  printer,
  onClose,
  onPrinted,
}: {
  printer: FarmPrinter
  onClose: () => void
  onPrinted: () => void | Promise<void>
}) {
  const [tab, setTab] = useState<'now' | 'machine' | 'everything'>('now')
  const [picking, setPicking] = useState(false)
  const [chosen, setChosen] = useState<FileChoice | null>(null)
  const [sent, setSent] = useState<string | null>(null)
  const state = machineState(printer)
  const left = duration(printer.remaining_minutes)

  // The picker and the confirmation are full panels of their own. Showing them
  // instead of this one rather than on top of it: stacked dialogs are two
  // Escape presses and an ambiguous backdrop, and there is nothing on the page
  // behind them worth reading while a file is being picked.
  if (picking) {
    return (
      <PrintFilePicker
        choice={{ printer_id: Number(printer.id), printer_models: [] }}
        onClose={() => setPicking(false)}
        onPick={(picked) => {
          setChosen(picked)
          setPicking(false)
        }}
      />
    )
  }
  if (chosen) {
    return (
      <PrintNow
        printer={printer}
        file={chosen}
        onClose={() => setChosen(null)}
        onDone={async (message) => {
          setChosen(null)
          setSent(message)
          await onPrinted()
        }}
      />
    )
  }

  return (
    <Modal open title={printer.name ?? `Printer ${printer.id}`} onClose={onClose} wide>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-ink-500">
        <Badge className={state.tone}>{state.label}</Badge>
        {printer.model ? <Badge>{printer.model}</Badge> : null}
        {printer.serial ? <span className="font-mono">{printer.serial}</span> : null}
        <Button className="ml-auto" onClick={() => setPicking(true)}>
          Print a file
        </Button>
      </div>

      {sent ? (
        <div className="mb-3">
          <Alert tone="success">{sent}</Alert>
        </div>
      ) : null}

      <div className="mb-3 flex gap-1 border-b border-ink-200">
        {(
          [
            ['now', 'Now'],
            ['machine', 'Machine'],
            ['everything', `Everything (${Object.keys(printer.reported ?? {}).length})`],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setTab(key)}
            className={cx(
              '-mb-px border-b-2 px-3 py-1.5 text-sm',
              tab === key
                ? 'border-ink-800 font-medium text-ink-900'
                : 'border-transparent text-ink-500 hover:text-ink-800',
            )}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === 'now' ? (
        <div className="space-y-3">
          {printer.camera ? (
            <LiveCamera printer={printer} />
          ) : (
            <p className="text-xs text-ink-400">No camera on this machine.</p>
          )}

          {printer.progress !== null &&
          (printer.progress > 0 || state.label === 'printing') ? (
            <div>
              <div className="h-2 overflow-hidden rounded-full bg-ink-200">
                <div
                  className="h-full rounded-full bg-sky-500"
                  style={{ width: `${Math.min(100, Math.max(0, printer.progress))}%` }}
                />
              </div>
              <p className="mt-1 text-xs text-ink-500">
                {printer.progress}%{left ? ` · ${left}` : ''}
                {printer.layers
                  ? ` · layer ${printer.layer ?? '—'}/${printer.layers}`
                  : ''}
              </p>
            </div>
          ) : null}

          <Readings
            rows={[
              ['Printing', printer.current_file],
              ['Started', printer.print_started_at],
              ['Nozzle', temperature(printer.nozzle_temp, printer.nozzle_target)],
              ['Bed', temperature(printer.bed_temp, printer.bed_target)],
              ['Chamber', temperature(printer.chamber_temp, null)],
              ['Part fan', printer.fan_speed === null ? null : `${printer.fan_speed}%`],
              ['Speed', printer.speed_level],
              ['Light', printer.chamber_light],
              ['Door', printer.door_open],
            ]}
          />

          {printer.error ? <Alert tone="error">{String(printer.error)}</Alert> : null}

          <div>
            <h3 className="text-sm font-semibold text-ink-800">
              PrintFlow plates on this machine
            </h3>
            {printer.plates.length ? (
              <div className="divide-y divide-ink-100">
                {printer.plates.map((job) => (
                  <div key={job.id} className="flex flex-wrap items-center gap-2 py-1.5 text-xs">
                    <Badge className={JOB_STATUS_CLASSES[job.status]}>{job.status}</Badge>
                    <span className="font-mono text-ink-900">#{job.order_number ?? '—'}</span>
                    <span className="min-w-0 flex-1 truncate text-ink-600">
                      {job.product_name ?? job.sku ?? '—'}
                    </span>
                    <span className="text-ink-500">
                      plate {job.plate_number ?? '—'} · {job.units_expected}u
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-xs text-ink-400">
                None. Anything printing now was started somewhere other than
                PrintFlow — the machine still reports it above.
              </p>
            )}
          </div>
        </div>
      ) : null}

      {tab === 'machine' ? (
        <div className="space-y-3">
          <Readings
            rows={[
              ['Model', printer.model],
              ['Serial', printer.serial],
              ['Firmware', printer.firmware],
              ['Address', printer.ip],
              ['Online', printer.online],
              ['Nozzle', printer.nozzle_diameter ? `${printer.nozzle_diameter} mm` : null],
              ['Nozzle type', printer.nozzle_type],
              ['Wi-Fi', printer.wifi_signal],
              ['Prints completed', printer.prints_completed],
              ['Total print time', printer.total_print_time],
            ]}
          />

          <div>
            <h3 className="text-sm font-semibold text-ink-800">Filament</h3>
            {printer.filament?.length ? (
              <div className="mt-1 grid gap-1.5 sm:grid-cols-2">
                {printer.filament.map((spool) => (
                  <div
                    key={String(spool.slot)}
                    className={cx(
                      'flex items-center gap-2 rounded-md px-2 py-1.5 text-xs',
                      lowSpool(spool.remaining) ? 'bg-amber-50' : 'bg-ink-50',
                    )}
                  >
                    <span className="w-3 shrink-0 font-medium text-ink-700">
                      {spool.slot}
                    </span>
                    <Swatch spool={spool} size="md" />
                    {/* Empty rather than "unknown": a slot that reports nothing
                        may simply have nothing in it, and calling that an
                        unknown filament sends somebody to check a spool that
                        is not there. */}
                    <span className="min-w-0 truncate text-ink-900">
                      {spool.brand ?? spool.type ?? (
                        <span className="text-ink-400">empty</span>
                      )}
                    </span>
                    {/* The type as well as the brand, since "PLA Matte" is the
                        reel and "PLA" is what the slicer needs to agree with. */}
                    {spool.brand && spool.type && !spool.brand.includes(spool.type) ? (
                      <span className="shrink-0 text-ink-500">{spool.type}</span>
                    ) : null}
                    {spool.colour ? (
                      <span className="min-w-0 truncate text-ink-500">
                        {spool.colour}
                      </span>
                    ) : null}
                    {spool.remaining === null ? null : (
                      <span
                        className={cx(
                          'ml-auto',
                          lowSpool(spool.remaining)
                            ? 'font-medium text-amber-800'
                            : 'text-ink-500',
                        )}
                      >
                        {spool.remaining}%
                        {lowSpool(spool.remaining) ? ' · nearly empty' : ''}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            ) : (
              // Empty is two very different things: a machine with nothing
              // loaded, and a build that keeps its AMS somewhere PrintFlow did
              // not look. Only the instance can tell them apart, so it is
              // asked rather than guessed at — the same move as the camera
              // panel. The reply below is the machine's own, unedited.
              <div className="mt-1 space-y-2">
                <p className="text-xs text-ink-500">
                  This machine reported no spools. That is either an empty
                  machine or an AMS somewhere PrintFlow has not been pointed
                  at — the reply below is what it actually sent. If there are
                  trays in it, that is a shape worth reporting.
                </p>
                <RawReplies endpoint="/api/printers/raw" />
              </div>
            )}
          </div>
        </div>
      ) : null}

      {tab === 'everything' ? (
        <div className="space-y-2">
          <p className="text-xs text-ink-500">
            Every field this Bambuddy sent for this machine, flattened and
            unedited. PrintFlow reads the handful it understands into the pages
            above; the rest is here so nothing the instance knows is hidden.
          </p>
          {Object.keys(printer.reported ?? {}).length ? (
            <div className="overflow-hidden rounded-md ring-1 ring-ink-200">
              <table className="w-full text-xs">
                <tbody className="divide-y divide-ink-100">
                  {Object.entries(printer.reported)
                    .sort(([a], [b]) => a.localeCompare(b))
                    .map(([key, value]) => (
                      <tr key={key} className="odd:bg-ink-50">
                        <td className="w-1/2 break-all px-2 py-1 font-mono text-ink-600">
                          {key}
                        </td>
                        <td className="break-all px-2 py-1 text-ink-900">
                          {value === null ? '—' : String(value)}
                        </td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Alert tone="warning">
              This machine's row held nothing PrintFlow could flatten. The raw
              replies below are what Bambuddy actually sent.
            </Alert>
          )}
          <RawReplies endpoint="/api/printers/raw" />
        </div>
      ) : null}
    </Modal>
  )
}

/** A row of readings, with the ones this build did not report left out.
 *
 *  Blank labels are worse than no label: a machine that reports no chamber
 *  temperature and a chamber at 0° look the same in a table of empty cells. */
function Readings({ rows }: { rows: [string, unknown][] }) {
  const shown = rows.filter(
    ([, value]) => value !== null && value !== undefined && value !== '',
  )
  if (!shown.length) return null
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs sm:grid-cols-3">
      {shown.map(([label, value]) => (
        <div key={label} className="min-w-0">
          <dt className="text-ink-500">{label}</dt>
          <dd className="truncate font-medium text-ink-900" title={String(value)}>
            {typeof value === 'boolean' ? (value ? 'yes' : 'no') : String(value)}
          </dd>
        </div>
      ))}
    </dl>
  )
}

/** The picture, bigger and much more often, because somebody is watching.
 *
 *  Once a second. It is still frames rather than a stream for the same reason
 *  as the cards, but here the cost is one machine for as long as the panel is
 *  open rather than the whole farm for as long as the tab is. */
function LiveCamera({ printer }: { printer: FarmPrinter }) {
  const [shown, setShown] = useState<string | null>(null)
  const [broken, setBroken] = useState(false)

  // Each frame is fetched out of sight and only swapped in once it has
  // arrived, so the picture never blinks through empty. And the next one is
  // asked for a second after the last one landed rather than on a metronome —
  // a slow camera should fall behind, not accumulate requests.
  useEffect(() => {
    let live = true
    let timer: number | undefined
    let frame = 0

    const next = () => {
      const url = `/api/printers/${printer.id}/camera?t=${Date.now()}.${frame++}`
      const image = new Image()
      image.onload = () => {
        if (!live) return
        setShown(url)
        setBroken(false)
        timer = window.setTimeout(next, 1000)
      }
      image.onerror = () => {
        if (!live) return
        setBroken(true)
        // Keep trying, slowly: a camera comes back when its machine wakes up.
        timer = window.setTimeout(next, 5000)
      }
      image.src = url
    }

    next()
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [printer.id])

  return (
    <div>
      {shown ? (
        <img
          src={shown}
          alt={`Camera on ${printer.name ?? 'this printer'}`}
          className="max-h-[50vh] w-full rounded-md bg-ink-900 object-contain"
        />
      ) : broken ? null : (
        <div className="flex h-64 items-center justify-center rounded-md bg-ink-900">
          <Spinner className="h-6 w-6" />
        </div>
      )}

      {broken ? (
        <div className="mt-2">
          <Alert tone="warning">
            The camera is not answering{shown ? ' any more' : ''}. It may be off,
            or this machine may not have one — PrintFlow keeps asking.
          </Alert>
        </div>
      ) : (
        <p className="mt-1 text-xs text-ink-500">
          A new picture every second, taken through PrintFlow — the camera
          itself is on the shop network and behind Bambuddy's key.
        </p>
      )}
    </div>
  )
}

/** Send a file straight to this machine — the reprint, the test piece.
 *
 *  A confirmation step rather than printing the moment a file is picked: this
 *  spends filament on a machine that may have something on it already, and the
 *  file list is close enough together that the wrong row is one scroll away.
 *  Nothing here touches an order; the plate is Bambuddy's from the moment it
 *  is queued, which is why the panel says so. */
function PrintNow({
  printer,
  file,
  onClose,
  onDone,
}: {
  printer: FarmPrinter
  file: FileChoice
  onClose: () => void
  onDone: (message: string) => void | Promise<void>
}) {
  const [plateNumber, setPlateNumber] = useState(1)
  const [copies, setCopies] = useState(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const busyNow = machineState(printer).label === 'printing'

  const send = async () => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.post<{ copies: number }>(
        `/api/printers/${printer.id}/print`,
        {
          bambuddy_archive_id: file.archive_id,
          bambuddy_file_path: file.file_path,
          name: file.name,
          plate_number: plateNumber,
          copies,
        },
      )
      await onDone(
        `Sent ${file.name} to ${printer.name ?? `printer ${printer.id}`}` +
          `${result.copies > 1 ? ` — ${result.copies} copies` : ''}.`,
      )
    } catch (err) {
      setError(errorMessage(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open title="Print this now" onClose={onClose}>
      <div className="space-y-3">
        <p className="text-sm text-ink-700">
          <span className="font-medium">{file.name}</span> on{' '}
          <span className="font-medium">{printer.name ?? `printer ${printer.id}`}</span>.
        </p>

        {busyNow ? (
          <Alert tone="warning">
            This machine is printing something. Bambuddy will hold the file in
            its queue until the plate on it now is finished and cleared.
          </Alert>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Plate" hint="Which plate inside the file.">
            <input
              className={inputClass}
              type="number"
              min={1}
              value={plateNumber}
              onChange={(event) => setPlateNumber(Math.max(1, Number(event.target.value) || 1))}
            />
          </Field>
          <Field label="Copies" hint="Queued one after another.">
            <input
              className={inputClass}
              type="number"
              min={1}
              max={50}
              value={copies}
              onChange={(event) =>
                setCopies(Math.min(50, Math.max(1, Number(event.target.value) || 1)))
              }
            />
          </Field>
        </div>

        {error ? <Alert tone="error">{error}</Alert> : null}

        <p className="text-xs text-ink-500">
          This is not attached to an order and will not count towards one. It
          goes into Bambuddy's queue for this machine, and the plate has to be
          cleared by hand like any other.
        </p>

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={send} disabled={busy}>
            {busy ? 'Sending…' : copies > 1 ? `Print ${copies} copies` : 'Print'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

/** Plates with no machine — waiting to be sent, or on a printer that is gone.
 *
 *  These are the ones nobody would otherwise see: a plate that found no machine
 *  of a model it can print on sits here indefinitely, and the fix is usually to
 *  plug something in or to attach another file to the product. */
function UnplacedPlates({
  plates,
  renderPlate,
}: {
  plates: QueueJob[]
  renderPlate: (job: QueueJob) => JSX.Element
}) {
  if (!plates.length) return null
  return (
    <Card className="p-3">
      <h2 className="text-sm font-semibold text-ink-800">Not on a machine</h2>
      <p className="text-xs text-ink-500">
        Waiting to be sent, or sent to a printer the farm no longer lists. A plate
        that names printer models with none on the farm stays here until one of
        them appears.
      </p>
      <div className="mt-2 divide-y divide-ink-200">{plates.map(renderPlate)}</div>
    </Card>
  )
}

function Plate({
  job,
  busy,
  onAct,
  compact = false,
}: {
  job: QueueJob
  busy: boolean
  onAct: (jobId: string, action: 'requeue' | 'cancel') => void
  compact?: boolean
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 py-1.5 text-xs">
      <Badge className={JOB_STATUS_CLASSES[job.status]}>{job.status}</Badge>
      <span className="font-mono text-ink-900">#{job.order_number ?? '—'}</span>
      <span className="min-w-0 flex-1 truncate text-ink-600" title={job.product_name ?? ''}>
        {job.product_name ?? job.sku ?? '—'}
      </span>
      {job.file_label ? (
        <span className="max-w-44 truncate text-ink-500" title={job.file_label}>
          {job.file_label}
        </span>
      ) : null}
      <span className="text-ink-500">
        plate {job.plate_number ?? '—'} · {job.units_expected}u
      </span>
      {job.printer_id === null && job.printer_models.length ? (
        <span className="text-ink-500">needs {job.printer_models.join(', ')}</span>
      ) : null}
      {compact ? null : (
        <span className="text-ink-400">
          {formatDateTime(job.completed_at ?? job.queued_at ?? job.created_at)}
        </span>
      )}
      <div className="flex gap-1">
        {['failed', 'cancelled'].includes(job.status) ? (
          <Button size="sm" onClick={() => onAct(job.id, 'requeue')} disabled={busy}>
            Re-queue
          </Button>
        ) : null}
        {['pending', 'queued', 'printing'].includes(job.status) ? (
          <Button size="sm" variant="ghost" onClick={() => onAct(job.id, 'cancel')} disabled={busy}>
            Cancel
          </Button>
        ) : null}
      </div>
      {job.error ? <p className="w-full text-red-700">{job.error}</p> : null}
    </div>
  )
}
