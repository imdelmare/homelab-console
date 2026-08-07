import { Rnd } from "react-rnd";
import type { ReactNode } from "react";
import { Button, Window, WindowContent, WindowHeader } from "react95";
import type { WindowState } from "../lib/types";

type WindowFrameProps = {
  window: WindowState;
  compact: boolean;
  isActive: boolean;
  children: ReactNode;
  onFocus: () => void;
  onClose: () => void;
  onMinimize: () => void;
  onMaximize: () => void;
  onChange: (patch: Partial<WindowState>) => void;
};

export function WindowFrame({
  window,
  compact,
  isActive,
  children,
  onFocus,
  onClose,
  onMinimize,
  onMaximize,
  onChange,
}: WindowFrameProps) {
  if (!window.isOpen || window.isMinimized) {
    return null;
  }

  const viewportMinWidth = Math.max(280, globalThis.window.innerWidth - 16);
  const viewportMinHeight = Math.max(200, globalThis.window.innerHeight - 48);

  return (
    <Rnd
      bounds=".desktop"
      className={`window-rnd ${compact ? "window-rnd-compact" : ""}`}
      size={{ width: window.width, height: window.height }}
      position={{ x: window.x, y: window.y }}
      minWidth={Math.min(window.minWidth, viewportMinWidth)}
      minHeight={Math.min(window.minHeight, viewportMinHeight)}
      maxWidth="calc(100vw - 16px)"
      maxHeight="calc(100vh - 48px)"
      dragHandleClassName="title-bar"
      disableDragging={compact || window.isMaximized}
      enableResizing={!compact && !window.isMaximized}
      style={{ zIndex: window.zIndex }}
      onMouseDown={(event: globalThis.MouseEvent) => {
        event.stopPropagation();
        onFocus();
      }}
      onDragStart={onFocus}
      onDragStop={(_, data) => onChange({ x: data.x, y: data.y })}
      onResizeStop={(_, __, ref, ___, position) => {
        onChange({
          width: ref.offsetWidth,
          height: ref.offsetHeight,
          x: position.x,
          y: position.y,
        });
      }}
    >
      <Window className={`window app-window react95-window ${isActive ? "active-window" : "inactive-window"}`}>
        <WindowHeader active={isActive} className="title-bar" onDoubleClick={compact ? undefined : onMaximize}>
          <span className="window-title">{window.title}</span>
          <div className="title-bar-controls" aria-label="Window controls" onMouseDown={(event) => event.stopPropagation()} onDoubleClick={(event) => event.stopPropagation()}>
            <Button type="button" aria-label="Minimize" title="Minimize" onClick={onMinimize}>_</Button>
            {!compact && (
              window.isMaximized ? (
                <Button
                  type="button"
                  aria-label="Restore window"
                  title="Restore"
                  onClick={onMaximize}
                >❐</Button>
              ) : (
                <Button
                  type="button"
                  aria-label="Maximize window"
                  title="Maximize"
                  onClick={onMaximize}
                >□</Button>
              )
            )}
            <Button type="button" aria-label="Close window" title="Close" onClick={onClose}>×</Button>
          </div>
        </WindowHeader>
        <WindowContent className="window-body app-window-body">{children}</WindowContent>
      </Window>
    </Rnd>
  );
}
