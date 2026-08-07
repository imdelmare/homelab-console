const APP_TIME_ZONE = "Europe/Rome";
const TIMEZONE_SUFFIX = /(?:z|[+-]\d{2}:?\d{2})$/i;

export function parseApiDate(iso: string): Date {
  const value = iso.trim();
  return new Date(TIMEZONE_SUFFIX.test(value) ? value : `${value}Z`);
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) {
    return "never";
  }
  const date = parseApiDate(iso);
  if (Number.isNaN(date.getTime())) {
    return iso;
  }
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "short",
    timeStyle: "medium",
    timeZone: APP_TIME_ZONE,
  }).format(date);
}

export function formatCountdown(iso: string, now: number = Date.now()): string {
  const remainingMs = parseApiDate(iso).getTime() - now;
  if (Number.isNaN(remainingMs) || remainingMs <= 0) {
    return "0:00";
  }
  const totalSeconds = Math.floor(remainingMs / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function shortId(id: string, length = 8): string {
  return id.length > length ? `${id.slice(0, length)}…` : id;
}
