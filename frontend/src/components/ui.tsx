import { useEffect, type ReactNode } from 'react'

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  size?: 'sm' | 'md'
}

export function Button({
  variant = 'secondary',
  size = 'md',
  className,
  ...props
}: ButtonProps) {
  const variants: Record<string, string> = {
    primary: 'bg-ink-900 text-white hover:bg-ink-800 disabled:bg-ink-400',
    secondary:
      'bg-white text-ink-800 ring-1 ring-ink-300 hover:bg-ink-50 disabled:text-ink-400',
    ghost: 'text-ink-600 hover:bg-ink-100 disabled:text-ink-300',
    danger: 'bg-red-600 text-white hover:bg-red-700 disabled:bg-red-300',
  }
  return (
    <button
      {...props}
      className={cx(
        'inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition',
        'disabled:cursor-not-allowed focus:outline-none focus-visible:ring-2 focus-visible:ring-ink-500',
        size === 'sm' ? 'px-2.5 py-1 text-xs' : 'px-3.5 py-2 text-sm',
        variants[variant],
        className,
      )}
    />
  )
}

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={cx('rounded-lg bg-white shadow-sm ring-1 ring-ink-200', className)}>
      {children}
    </div>
  )
}

export function Badge({
  className,
  children,
}: {
  className?: string
  children: ReactNode
}) {
  return (
    <span
      className={cx(
        'inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ring-inset',
        className ?? 'bg-ink-100 text-ink-700 ring-ink-300',
      )}
    >
      {children}
    </span>
  )
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: ReactNode
  children: ReactNode
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-ink-700">{label}</span>
      {children}
      {hint ? <span className="mt-1 block text-xs text-ink-500">{hint}</span> : null}
    </label>
  )
}

export const inputClass =
  'w-full rounded-md border-0 bg-white px-3 py-2 text-sm text-ink-900 ring-1 ring-inset ' +
  'ring-ink-300 placeholder:text-ink-400 focus:ring-2 focus:ring-inset focus:ring-ink-600'

export function Alert({
  tone = 'error',
  children,
  action,
}: {
  tone?: 'error' | 'warning' | 'info' | 'success'
  children: ReactNode
  action?: ReactNode
}) {
  const tones = {
    error: 'bg-red-50 text-red-800 ring-red-200',
    warning: 'bg-amber-50 text-amber-900 ring-amber-200',
    info: 'bg-sky-50 text-sky-900 ring-sky-200',
    success: 'bg-emerald-50 text-emerald-900 ring-emerald-200',
  }
  return (
    <div
      className={cx(
        'flex flex-wrap items-center justify-between gap-2 rounded-md px-3 py-2 text-sm ring-1',
        tones[tone],
      )}
    >
      <div className="min-w-0 flex-1">{children}</div>
      {action}
    </div>
  )
}

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      className={cx(
        'inline-block h-4 w-4 animate-spin rounded-full border-2 border-ink-300 border-t-ink-700',
        className,
      )}
    />
  )
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="rounded-lg border border-dashed border-ink-300 px-6 py-10 text-center">
      <p className="text-sm font-medium text-ink-700">{title}</p>
      {description ? <p className="mt-1 text-sm text-ink-500">{description}</p> : null}
      {action ? <div className="mt-4 flex justify-center">{action}</div> : null}
    </div>
  )
}

export function Modal({
  open,
  title,
  onClose,
  children,
  wide,
}: {
  open: boolean
  title: string
  onClose: () => void
  children: ReactNode
  wide?: boolean
}) {
  useEffect(() => {
    if (!open) return
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-ink-900/40 p-0 sm:items-center sm:p-4">
      <div
        className={cx(
          'flex max-h-[92vh] w-full flex-col overflow-hidden rounded-t-xl bg-white shadow-xl sm:rounded-xl',
          wide ? 'sm:max-w-3xl' : 'sm:max-w-lg',
        )}
      >
        <div className="flex items-center justify-between border-b border-ink-200 px-4 py-3">
          <h2 className="text-sm font-semibold text-ink-900">{title}</h2>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close">
            ✕
          </Button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">{children}</div>
      </div>
    </div>
  )
}
