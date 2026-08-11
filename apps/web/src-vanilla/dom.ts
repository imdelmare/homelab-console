type Child = Node | string | null | undefined | false;

type Attributes = Record<string, string | number | boolean | null | undefined>;

export function element<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attributes: Attributes = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);

  for (const [name, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "className") {
      node.className = String(value);
    } else if (name === "disabled") {
      (node as HTMLButtonElement).disabled = Boolean(value);
    } else if (name === "checked") {
      (node as HTMLInputElement).checked = Boolean(value);
    } else {
      node.setAttribute(name, value === true ? "" : String(value));
    }
  }

  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(child));
  }

  return node;
}

export function replaceChildren(target: Element, ...children: Child[]): void {
  target.replaceChildren(
    ...children
      .filter((child): child is Node | string => child !== null && child !== undefined && child !== false)
      .map((child) => (child instanceof Node ? child : document.createTextNode(child))),
  );
}

export function button(label: string, className = "button"): HTMLButtonElement {
  return element("button", { type: "button", className }, label);
}
