// Copy-only projection (exploration.project_tree): focus one branch and hide
// collapsed descendants. Node records are shared, never modified, so every
// measurement is identical whatever is visible.
import type { ChartData, ChartNode } from "./types";

export interface Projection {
  root: ChartNode;
  /** Visible operators in pre-order. */
  visible: ChartNode[];
  byId: Map<string, ChartNode>;
  /** Visible inputs of a visible operator, in reported order. */
  children(node: ChartNode): ChartNode[];
  /** Number of descendants hidden by collapsing this operator. */
  hidden(node: ChartNode): number;
}

export function index(data: ChartData): Map<string, ChartNode> {
  const byId = new Map<string, ChartNode>();
  for (const node of data.nodes) byId.set(node.id, node);
  return byId;
}

export function project(data: ChartData, focusId = "", collapsedIds: string[] = []): Projection {
  const byId = index(data);
  const root = byId.get(focusId || data.root_id);
  if (!root) throw new Error(focusId ? "Unknown focus operator." : "Chart data has no root operator.");
  const collapsed = new Set(collapsedIds);
  const inputs = (node: ChartNode) =>
    node.children.map((id) => byId.get(id)).filter((child): child is ChartNode => !!child);
  const descendants = (node: ChartNode): number =>
    inputs(node).reduce((sum, child) => sum + 1 + descendants(child), 0);
  const hiddenCounts = new Map<string, number>();
  const visible: ChartNode[] = [];
  const walk = (node: ChartNode) => {
    visible.push(node);
    if (collapsed.has(node.id) && node.children.length) {
      hiddenCounts.set(node.id, descendants(node));
      return;
    }
    inputs(node).forEach(walk);
  };
  walk(root);
  return {
    root,
    visible,
    byId,
    children: (node) => (hiddenCounts.has(node.id) ? [] : inputs(node)),
    hidden: (node) => hiddenCounts.get(node.id) || 0,
  };
}
