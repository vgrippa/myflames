// Selection bridge between an embedded chart and its parent window.
//   parent -> chart: {type: "myflames-select", node_id}
//   chart -> parent: {type: "myflames-node", node_id}
// Messages from any window other than the parent are ignored, as are
// malformed payloads. Unknown IDs are the selection's responsibility.

export interface BridgeWindow {
  readonly parent: unknown;
  addEventListener(type: "message", listener: (event: MessageEvent) => void): void;
  removeEventListener(type: "message", listener: (event: MessageEvent) => void): void;
}

export interface Bridge {
  notify(id: string): void;
  dispose(): void;
}

interface Poster {
  postMessage(message: unknown, targetOrigin: string): void;
}

export function createBridge(win: BridgeWindow, onSelect: (id: string) => void): Bridge {
  const listener = (event: MessageEvent) => {
    const data = event.data as { type?: unknown; node_id?: unknown } | null;
    if (event.source !== win.parent) return;
    if (!data || data.type !== "myflames-select" || typeof data.node_id !== "string") return;
    onSelect(data.node_id);
  };
  win.addEventListener("message", listener);
  return {
    notify(id: string) {
      const parent = win.parent as Poster | null;
      if (!parent || parent === (win as unknown) || typeof id !== "string") return;
      // Opaque sandboxed frames cannot name the parent origin; payloads
      // carry only public operator IDs.
      parent.postMessage({ type: "myflames-node", node_id: id }, "*");
    },
    dispose() {
      win.removeEventListener("message", listener);
    },
  };
}
