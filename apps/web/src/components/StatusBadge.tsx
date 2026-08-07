type StatusTone = "success" | "warning" | "danger" | "neutral";

type StatusBadgeProps = {
  label: string;
  tone?: StatusTone;
  className?: string;
};

export function StatusBadge({ label, tone = "neutral", className = "" }: StatusBadgeProps) {
  return (
    <span className={`status-badge status-badge-${tone} ${className}`.trim()}>
      <span className="status-badge-dot" aria-hidden="true" />
      {label}
    </span>
  );
}
