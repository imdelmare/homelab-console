import { Menu, X } from "lucide-react";
import { useEffect } from "react";
import type { ComponentType, ReactNode, SVGProps } from "react";
import type { AppId } from "../lib/types";

type MobileApp = {
  id: AppId;
  title: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  tone: string;
};

type MobileShellProps = {
  apps: MobileApp[];
  activeAppId: AppId;
  children: ReactNode;
  username: string;
  isDrawerOpen: boolean;
  badges?: Partial<Record<AppId, number | null>>;
  onNavigate: (id: AppId) => void;
  onDrawerChange: (open: boolean) => void;
  onLogout: () => void;
};

const primaryNavigation: AppId[] = ["overview", "tasks", "watchers"];

export function MobileShell({
  apps,
  activeAppId,
  children,
  username,
  isDrawerOpen,
  badges = {},
  onNavigate,
  onDrawerChange,
  onLogout,
}: MobileShellProps) {
  const activeApp = apps.find((app) => app.id === activeAppId) ?? apps[0];

  useEffect(() => {
    if (!isDrawerOpen) return;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") onDrawerChange(false);
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [isDrawerOpen, onDrawerChange]);

  function navigate(id: AppId) {
    onNavigate(id);
    onDrawerChange(false);
  }

  return (
    <main className="mobile-shell">
      <header className="mobile-app-bar">
        <button type="button" aria-label="Open menu" aria-expanded={isDrawerOpen} aria-controls="mobile-navigation" onClick={() => onDrawerChange(true)}>
          <Menu aria-hidden="true" />
        </button>
        <span className={`mobile-app-bar-icon icon-glyph-${activeApp.tone}`}><activeApp.icon width={24} height={24} aria-hidden="true" /></span>
        <strong>{activeApp.title}</strong>
      </header>

      <section className="mobile-page" aria-label={activeApp.title}>
        {children}
      </section>

      <nav className="mobile-bottom-nav" aria-label="Primary navigation">
        {primaryNavigation.map((id) => {
          const app = apps.find((candidate) => candidate.id === id)!;
          const badge = badges[id];
          return (
            <button type="button" key={id} aria-current={activeAppId === id ? "page" : undefined} onClick={() => navigate(id)}>
              <span className="mobile-nav-icon"><app.icon width={22} height={22} aria-hidden="true" />{badge != null && badge > 0 && <mark>{badge}</mark>}</span>
              <span>{app.title}</span>
            </button>
          );
        })}
        <button type="button" aria-expanded={isDrawerOpen} aria-controls="mobile-navigation" onClick={() => onDrawerChange(true)}>
          <Menu width={22} height={22} aria-hidden="true" />
          <span>Menu</span>
        </button>
      </nav>

      {isDrawerOpen && (
        <div className="mobile-drawer-backdrop" role="presentation" onClick={() => onDrawerChange(false)}>
          <aside id="mobile-navigation" className="mobile-drawer" role="dialog" aria-modal="true" aria-label="All sections" onClick={(event) => event.stopPropagation()}>
            <div className="mobile-drawer-heading">
              <div><strong>Homelab Console</strong><span>{username}</span></div>
              <button type="button" aria-label="Close menu" onClick={() => onDrawerChange(false)}><X aria-hidden="true" /></button>
            </div>
            <nav aria-label="Sections">
              {apps.map((app) => {
                const badge = badges[app.id];
                return (
                  <button type="button" key={app.id} aria-current={activeAppId === app.id ? "page" : undefined} onClick={() => navigate(app.id)}>
                    <span className={`mobile-drawer-icon icon-glyph-${app.tone}`}><app.icon width={24} height={24} aria-hidden="true" /></span>
                    <span>{app.title}</span>
                    {badge != null && badge > 0 && <mark>{badge}</mark>}
                  </button>
                );
              })}
            </nav>
            <button className="mobile-logout" type="button" onClick={onLogout}>Sign out</button>
          </aside>
        </div>
      )}
    </main>
  );
}
