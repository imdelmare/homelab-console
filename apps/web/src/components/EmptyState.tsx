import type { ReactNode } from "react";
import { Button, Frame } from "react95";

type EmptyStateProps = {
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
  icon?: ReactNode;
};

export function EmptyState({ title, description, actionLabel, onAction, icon }: EmptyStateProps) {
  return (
    <Frame variant="field" className="empty-state" role="status">
      {icon && <span className="empty-state-icon" aria-hidden="true">{icon}</span>}
      <strong>{title}</strong>
      <p>{description}</p>
      {actionLabel && onAction && <Button onClick={onAction}>{actionLabel}</Button>}
    </Frame>
  );
}
