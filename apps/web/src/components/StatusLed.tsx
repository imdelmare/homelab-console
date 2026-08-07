import type { ProviderStatus } from "../lib/types";

const STATUS_LABEL: Record<ProviderStatus, string> = {
  healthy: "Operational",
  degraded: "Warning",
  unreachable: "Unreachable",
  unavailable: "Unavailable",
  misconfigured: "Misconfigured",
  unknown: "Unknown status",
};

type StatusLedProps = {
  status: string;
};

export function StatusLed({ status }: StatusLedProps) {
  const label = STATUS_LABEL[status as ProviderStatus] ?? status;
  return (
    <span className={`status-led status-led-${status}`}>
      <span className="status-led-dot" aria-hidden="true" />
      {label}
    </span>
  );
}
