import { LogOut } from "lucide-react";
import type { ComponentType, SVGProps } from "react";
import { MenuList, MenuListItem, Separator, Window } from "react95";
import type { AppId } from "../lib/types";

type StartMenuProps = {
  isOpen: boolean;
  apps: Array<{ id: AppId; title: string; icon: ComponentType<SVGProps<SVGSVGElement>>; tone?: string }>;
  username: string;
  onOpenApp: (id: AppId) => void;
  onLogout: () => void;
};

export function StartMenu({ isOpen, apps, username, onOpenApp, onLogout }: StartMenuProps) {
  if (!isOpen) {
    return null;
  }

  return (
    <Window id="start-menu" className="start-menu react95-start-menu" role="navigation" aria-label="Start menu" onMouseDown={(event) => event.stopPropagation()}>
      <div className="start-menu-brand">Homelab</div>
      <MenuList className="start-menu-items" role="menu" fullWidth>
        {apps.map((app) => (
          <MenuListItem key={app.id} onClick={() => onOpenApp(app.id)}>
            <span className={`menu-glyph icon-glyph-${app.tone ?? app.id}`}><app.icon width={22} height={22} aria-hidden="true" /></span>
            <span className="start-menu-label">{app.title}</span>
          </MenuListItem>
        ))}
        <Separator className="start-menu-separator" />
        <MenuListItem onClick={onLogout}>
          <span className="menu-glyph"><LogOut size={18} strokeWidth={2.2} /></span>
          <span className="start-menu-label">Sign out {username}</span>
        </MenuListItem>
      </MenuList>
    </Window>
  );
}
