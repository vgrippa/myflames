// Lightweight module-level bridge for the app-level WhyDisabledHost so
// privileges.ts stays small in the entry chunk while WhyDisabled.tsx loads on
// demand (or during idle preload).
export type WhyDisabledRequest = {
  anchor: HTMLElement;
  reason: string;
  /** Backend-supplied advisory GRANT statement, or null when inapplicable. */
  grantSql?: string | null;
  /** Optional label for the dialog and GRANT Commands block. */
  label?: string;
};

export type WhyHostApi = {
  open: (request: WhyDisabledRequest) => void;
  close: () => void;
};

let registeredHost: WhyHostApi | null = null;
let pendingRequest: WhyDisabledRequest | null = null;
const demandListeners = new Set<() => void>();

export function registerWhyHost(api: WhyHostApi): () => void {
  registeredHost = api;
  if (pendingRequest) {
    const req = pendingRequest;
    pendingRequest = null;
    api.open(req);
  }
  return () => {
    if (registeredHost === api) registeredHost = null;
  };
}

export function onWhyDemand(listener: () => void): () => void {
  demandListeners.add(listener);
  if (pendingRequest) listener();
  return () => {
    demandListeners.delete(listener);
  };
}

/** Open (or toggle closed, when called again for the same button) the single
 * app-level WhyDisabled message anchored to the clicked disabled button. */
export function openWhyDisabled(request: WhyDisabledRequest): void {
  if (registeredHost) {
    registeredHost.open(request);
    return;
  }
  if (!request.reason || !request.anchor.isConnected) return;
  if (pendingRequest?.anchor === request.anchor) {
    pendingRequest = null;
    return;
  }
  pendingRequest = request;
  for (const listener of demandListeners) listener();
}

/** Close the app-level WhyDisabled message if open. */
export function closeWhyDisabled(): void {
  pendingRequest = null;
  registeredHost?.close();
}
