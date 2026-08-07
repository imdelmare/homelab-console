import type { ReactNode } from "react";
import { Button, Window, WindowContent, WindowHeader } from "react95";

type ConfirmDialogProps = {
  title: string;
  message: string;
  confirmLabel?: string;
  busy?: boolean;
  confirmDisabled?: boolean;
  children?: ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
};

export function ConfirmDialog({
  title,
  message,
  confirmLabel = "OK",
  busy = false,
  confirmDisabled = false,
  children,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <div className="modal-overlay" onMouseDown={(event) => event.stopPropagation()}>
      <Window className="window confirm-dialog react95-window" role="dialog" aria-modal="true">
        <WindowHeader active className="title-bar"><span className="window-title">{title}</span></WindowHeader>
        <WindowContent className="window-body confirm-dialog-body">
          <p>{message}</p>
          {children}
          <div className="dialog-actions">
            <Button onClick={onConfirm} disabled={busy || confirmDisabled}>{confirmLabel}</Button>
            <Button onClick={onCancel} disabled={busy}>Cancel</Button>
          </div>
        </WindowContent>
      </Window>
    </div>
  );
}
