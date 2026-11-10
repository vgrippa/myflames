// The connection in use, for surfaces that are not bound to a view's read
// (the Settings menu's Client tools dialog): main.tsx provides it, dialog
// code reads it. The object is the same one live reads send; it is never
// stored anywhere else from here.
import { createContext, createElement, useContext, type PropsWithChildren } from "react";
import type { Connection } from "./api";

const ConnectionInUseContext = createContext<Connection | null>(null);

export function ConnectionInUseProvider({ connection, connected = true, children }: PropsWithChildren<{
  connection: Connection;
  /** False while no connection is in use: the context reads null then. */
  connected?: boolean;
}>) {
  return createElement(ConnectionInUseContext.Provider, { value: connected ? connection : null }, children);
}

/** The connection in use, or null when none is (not connected yet). */
export function useConnectionInUse(): Connection | null {
  return useContext(ConnectionInUseContext);
}
