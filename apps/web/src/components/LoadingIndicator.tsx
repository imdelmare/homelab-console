import { useEffect, useState } from "react";
import { ProgressBar } from "react95";

type LoadingIndicatorProps = {
  label?: string;
  size?: number;
};

export function LoadingIndicator({ label = "Loading…", size = 30 }: LoadingIndicatorProps) {
  const [value, setValue] = useState(18);

  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const interval = window.setInterval(() => setValue((current) => current >= 92 ? 18 : current + 11), 280);
    return () => window.clearInterval(interval);
  }, []);

  return (
    <div className="loading-indicator" role="status" aria-live="polite">
      <span>{label}</span>
      <ProgressBar className="loading-progress" variant="tile" value={value} hideValue style={{ maxWidth: `${Math.max(150, size * 7)}px` }} />
    </div>
  );
}
