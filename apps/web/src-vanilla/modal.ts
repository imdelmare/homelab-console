import { button, element } from "./dom";

export function confirmAction(title: string, message: string, confirmLabel = "Confirm"): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = element("dialog", { className: "action-dialog", "aria-labelledby": "action-dialog-title" });
    const cancel = button("Cancel", "quiet-button");
    const confirm = button(confirmLabel, "primary-action danger-action");
    let settled = false;
    const finish = (result: boolean) => {
      if (settled) return;
      settled = true;
      dialog.close();
      dialog.remove();
      resolve(result);
    };
    cancel.addEventListener("click", () => finish(false));
    confirm.addEventListener("click", () => finish(true));
    dialog.addEventListener("cancel", (event) => { event.preventDefault(); finish(false); });
    dialog.append(element("p", { className: "eyebrow" }, "Operator confirmation"), element("h2", { id: "action-dialog-title" }, title), element("p", {}, message), element("div", { className: "dialog-actions" }, cancel, confirm));
    document.body.append(dialog);
    dialog.showModal();
    cancel.focus();
  });
}
