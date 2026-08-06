// Small presentational components shared by more than one desktop app.
import {
  Activity,
  BatteryCharging,
  Camera,
  Cloud,
  Gauge,
  Home,
  Phone,
  RadioTower,
  Router,
  Server,
  Shield,
  ShieldCheck,
} from "lucide-react";
import { text } from "../lib/ui";
import type { JsonRecord } from "../lib/ui";

type ProviderIconName =
  | "activity"
  | "battery"
  | "camera"
  | "cloud"
  | "dns"
  | "gauge"
  | "home"
  | "phone"
  | "radio"
  | "router"
  | "server"
  | "shield";

const PROVIDER_ICON: Record<string, ProviderIconName> = {
  adguard: "dns",
  asterisk: "phone",
  cloudflaretunnel: "cloud",
  emqx: "radio",
  frigate: "camera",
  fritzbox_primary: "router",
  fritzbox_secondary: "router",
  homeassistant: "home",
  mikrotik: "router",
  nextcloud: "cloud",
  nutups: "battery",
  opnsense: "shield",
  pbs: "server",
  proxmox: "server",
  uptimekuma: "gauge",
  vps: "cloud",
  zerotier: "radio",
};

export function ProviderIcon({ providerId }: { providerId: string }) {
  const iconName = PROVIDER_ICON[providerId] ?? "activity";
  const Icon = {
    activity: Activity,
    battery: BatteryCharging,
    camera: Camera,
    cloud: Cloud,
    dns: ShieldCheck,
    gauge: Gauge,
    home: Home,
    phone: Phone,
    radio: RadioTower,
    router: Router,
    server: Server,
    shield: Shield,
  }[iconName];

  return (
    <span className={`provider-icon provider-icon-${iconName}`} aria-hidden="true">
      <Icon size={21} strokeWidth={2.2} />
    </span>
  );
}

export function ResultTable({
  columns,
  rows,
}: {
  columns: Array<{ key: string; label: string; render?: (row: JsonRecord) => string }>;
  rows: JsonRecord[];
}) {
  return (
    <div className="result-table-wrap">
      <table className="result-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key}>{column.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${text(row.id ?? row.name ?? row.vmid, "row")}-${index}`}>
              {columns.map((column) => (
                <td key={column.key}>{column.render ? column.render(row) : text(row[column.key])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function KeyValueGrid({ items }: { items: Array<[string, unknown]> }) {
  return (
    <dl className="kv-grid">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>{Array.isArray(value) ? value.map((item) => text(item)).join(" · ") : text(value)}</dd>
        </div>
      ))}
    </dl>
  );
}
