// A deterministic, read-only projection of topology-group-1.0. Cluster
// membership and link verification come exclusively from the report.
import { useId, useMemo } from "react";
import type { Selection } from "./kit";
import { ServerGlyph } from "./TopologyPresentation";
import type { MapCluster, MapEdge, MapNode, TopologyMap as MapReport } from "./topologyApi";

const CARD_W = 220;
const CARD_H = 126;
const GAP = 36;
const FLOW_GAP = 70;
const PAD = 20;
const CLUSTER_HEAD = 54;
const ROLE: Record<string, string> = {
  primary: "Primary", secondary: "Secondary", node: "Node", source: "Source",
  replica: "Replica", standalone: "Standalone", proxy: "Proxy", cache: "Cache",
};
const OUTCOME = { read: "Read", failed: "Failed", not_reached: "Not reached" };
const CLUSTER: Record<MapCluster["kind"], string> = {
  group_replication: "Group Replication", innodb_cluster: "InnoDB Cluster", galera: "Galera", pxc: "Percona XtraDB Cluster",
};

export function mapAddress(node: MapNode): string {
  if (!node.host) return "Address not reported";
  const bare = node.host.replace(/^\[|\]$/g, "");
  const host = bare.includes(":") ? `[${bare}]` : bare;
  return node.port === null ? host : `${host}:${node.port}`;
}

/** The link's name. An alias link and a MariaDB ID-only replication link
 * override the backend label: the alias keeps its own sentence, and the
 * backend labels the ID-only link with its verification sentence, which the
 * verification segment already carries. */
export function mapEdgeLabel(edge: MapEdge): string {
  if (edge.kind === "alias") return "Same server (alias)";
  if (edge.kind === "replication" && edge.evidence === "server_id" && !edge.verified) return "Replication";
  return edge.label || edge.kind.replace(/_/g, " ");
}

/** How certain the link is, in words. A MariaDB link matched only by server
 * ID says so; IDs can collide, so it is never called verified. */
export function mapEdgeVerification(edge: MapEdge): string {
  if (edge.verified) return "verified";
  if (edge.evidence === "server_id") return "server ID match, not verified";
  return "address match, not verified";
}

export function mapEdgeText(edge: MapEdge): string {
  if (edge.kind === "alias") return mapEdgeLabel(edge);
  return [mapEdgeLabel(edge), edge.state || "State not reported",
    mapEdgeVerification(edge)].join(" · ");
}

function stateTone(state: string | null): string {
  switch ((state || "").toLowerCase()) {
    case "online": case "running": case "reachable": case "up": case "healthy": return "ok";
    case "stopped": case "shunned": case "offline": case "offline_hard": case "down": case "unhealthy": case "error": return "problem";
    case "connecting": case "recovering": case "offline_soft": return "watch";
    default: return "unknown";
  }
}

type Point = { x: number; y: number };
type PlacedNode = Point & { node: MapNode };
type Block = Point & { id: string; cluster?: MapCluster; nodes: PlacedNode[]; width: number; height: number; tier: number };

/** Collapse strongly connected components before ranking the DAG. Circular
 * replication stays on one level; a chain's deepest replicas stay lowest.
 * Neither the report nor its node order is mutated. */
function ranks(ids: string[], links: [string, string][]): Map<string, number> {
  const next = new Map(ids.map((id) => [id, [] as string[]]));
  for (const [from, to] of links) if (next.has(from) && next.has(to) && from !== to) next.get(from)!.push(to);
  let serial = 0;
  const index = new Map<string, number>();
  const low = new Map<string, number>();
  const stack: string[] = [];
  const active = new Set<string>();
  const component = new Map<string, number>();
  let count = 0;
  const visit = (id: string) => {
    index.set(id, serial);
    low.set(id, serial++);
    stack.push(id);
    active.add(id);
    for (const target of next.get(id)!) {
      if (!index.has(target)) {
        visit(target);
        low.set(id, Math.min(low.get(id)!, low.get(target)!));
      } else if (active.has(target)) low.set(id, Math.min(low.get(id)!, index.get(target)!));
    }
    if (low.get(id) === index.get(id)) {
      let member: string;
      do {
        member = stack.pop()!;
        active.delete(member);
        component.set(member, count);
      } while (member !== id);
      count++;
    }
  };
  ids.forEach((id) => { if (!index.has(id)) visit(id); });
  const parents = Array.from({ length: count }, () => new Set<number>());
  for (const [from, targets] of next) for (const to of targets) {
    const source = component.get(from)!;
    const target = component.get(to)!;
    if (source !== target) parents[target].add(source);
  }
  const depth = new Map<number, number>();
  const level = (id: number): number => {
    if (!depth.has(id)) depth.set(id, Math.max(0, ...[...parents[id]].map((p) => level(p) + 1)));
    return depth.get(id)!;
  };
  return new Map(ids.map((id) => [id, level(component.get(id)!)]));
}

function layout(map: MapReport) {
  const parallelCounts = new Map<string, number>();
  for (const edge of map.edges) {
    const key = JSON.stringify([edge.from, edge.to]);
    parallelCounts.set(key, (parallelCounts.get(key) || 0) + 1);
  }
  // ProxySQL can report several hostgroups for the same backend. Reserve
  // enough space for each link's state label instead of painting them over.
  const flowGap = Math.max(FLOW_GAP, ...[...parallelCounts.values()].map((count) => count * 20 + 20));
  const replication = map.edges.filter((edge) => edge.kind === "replication").map((edge): [string, string] => [edge.from, edge.to]);
  const claimed = new Set<string>();
  const blocks: Block[] = [];
  const makeBlock = (nodes: MapNode[], cluster?: MapCluster): Block => {
    const depths = ranks(nodes.map((node) => node.id), replication);
    const rows = new Map<number, MapNode[]>();
    nodes.forEach((node) => {
      const row = depths.get(node.id)!;
      rows.set(row, [...(rows.get(row) || []), node]);
    });
    const width = Math.max(1, ...[...rows.values()].map((row) => row.length)) * (CARD_W + GAP) - GAP + (cluster ? PAD * 2 : 0);
    const placed: PlacedNode[] = [];
    for (const [rank, row] of rows) row.forEach((node, i) => placed.push({
      node, x: (width - (row.length * (CARD_W + GAP) - GAP)) / 2 + i * (CARD_W + GAP),
      y: (cluster ? CLUSTER_HEAD : 0) + rank * (CARD_H + flowGap),
    }));
    placed.sort((a, b) => a.y - b.y || a.x - b.x);
    const first = nodes[0];
    const tier = first?.kind === "member" && first.role === "proxy" ? 0
      : first?.kind === "member" && first.role === "cache" ? 1 : 2;
    return { id: cluster ? `cluster:${cluster.id}` : `node:${first.id}`, cluster, nodes: placed, width,
      height: placed.length ? Math.max(...placed.map((item) => item.y)) + CARD_H + (cluster ? PAD : 0) : CLUSTER_HEAD + 40,
      x: 0, y: 0, tier };
  };
  // The backend may include unconsented peers in cluster.members. They remain
  // dashed peers inside that box and never acquire saved-member actions.
  for (const cluster of map.clusters) {
    const nodes = map.nodes.filter((node) => cluster.members.includes(node.id) && !claimed.has(node.id));
    nodes.forEach((node) => claimed.add(node.id));
    blocks.push(makeBlock(nodes, cluster));
  }
  map.nodes.forEach((node) => { if (!claimed.has(node.id)) blocks.push(makeBlock([node])); });
  const owner = new Map(blocks.flatMap((block) => block.nodes.map(({ node }) => [node.id, block.id] as const)));
  const database = blocks.filter((block) => block.tier === 2);
  const depths = ranks(database.map((block) => block.id), replication.map(([from, to]) => [owner.get(from)!, owner.get(to)!]));
  const rows = new Map<number, Block[]>();
  blocks.forEach((block) => {
    const row = block.tier === 2 ? 2 + depths.get(block.id)! : block.tier;
    rows.set(row, [...(rows.get(row) || []), block]);
  });
  const width = Math.max(500, ...[...rows.values()].map((row) => row.reduce((sum, block) => sum + block.width + GAP, -GAP))) + PAD * 2;
  let y = PAD;
  const headings: { y: number; label: string }[] = [];
  for (const rank of [...rows.keys()].sort((a, b) => a - b)) {
    const row = rows.get(rank)!;
    headings.push({ y, label: rank === 0 ? "ProxySQL routing" : rank === 1 ? "Readyset caches" : rank === 2 ? "Database servers" : `Replicas · level ${rank - 2}` });
    y += 32;
    const rowWidth = row.reduce((sum, block) => sum + block.width + GAP, -GAP);
    // Keep small upstream tiers in the first visible part of a wide group.
    // Centering them across all 16 members would put them offscreen on open.
    const anchorWidth = Math.min(width - PAD * 2, 3 * CARD_W + 2 * GAP + 2 * PAD);
    let x = PAD + Math.max(0, (anchorWidth - rowWidth) / 2);
    row.forEach((block) => { block.x = x; block.y = y; x += block.width + GAP; });
    y += Math.max(...row.map((block) => block.height)) + flowGap;
  }
  blocks.sort((a, b) => a.y - b.y || a.x - b.x);
  const positions = new Map(blocks.flatMap((block) => block.nodes.map((item) => [item.node.id, {
    x: block.x + item.x, y: block.y + item.y,
  }] as const)));
  const hasCircularRow = map.edges.some((edge) => {
    const from = positions.get(edge.from);
    const to = positions.get(edge.to);
    return from && to && from.y === to.y;
  });
  // Leave room below circular links and beside upward links.
  return { blocks, positions, headings, width: width + 100,
    height: Math.max(180, y - flowGap + PAD + (hasCircularRow ? flowGap : 0)) };
}

function edgePath(from: Point, to: Point, lane: number, slot: number, gutter: number, sourceBlock: Block, targetBlock: Block) {
  const offset = Math.max(-CARD_W / 3, Math.min(CARD_W / 3, slot * 14));
  const x1 = from.x + CARD_W / 2 + offset;
  const x2 = to.x + CARD_W / 2 + offset;
  const y1 = from.y + CARD_H;
  if (from.y === to.y) {
    const bottom = y1 + 24 + lane * 9 + Math.abs(slot) * 20;
    return { d: `M${x1},${y1} V${bottom} H${x2} V${to.y + CARD_H}`, x: (x1 + x2) / 2, y: bottom };
  }
  if (to.y > y1) {
    const base = sourceBlock !== targetBlock
      ? (sourceBlock.y + sourceBlock.height + targetBlock.y) / 2
      : y1 + (to.y - y1) / 2;
    const middle = base + slot * 20;
    if (sourceBlock.cluster && sourceBlock !== targetBlock) {
      // Exit between internal member rows before descending outside the box.
      const side = sourceBlock.x + sourceBlock.width + GAP / 2;
      return { d: `M${x1},${y1} V${y1 + 12} H${side} V${middle} H${x2} V${to.y}`, x: (side + x2) / 2, y: middle };
    }
    return { d: `M${x1},${y1} V${middle} H${x2} V${to.y}`, x: (x1 + x2) / 2, y: middle };
  }
  const startY = from.y + CARD_H / 2;
  const endY = to.y + CARD_H / 2;
  return { d: `M${from.x + CARD_W},${startY} H${gutter} V${endY} H${to.x + CARD_W}`,
    x: gutter, y: (startY + endY) / 2 };
}

export function TopologyMap({ map, selection }: { map: MapReport; selection: Selection }) {
  const diagram = useMemo(() => layout(map), [map]);
  const arrow = `topology-arrow-${useId().replace(/:/g, "")}`;
  if (!map.nodes.length && !map.clusters.length) return <p className="hint">No servers were reported.</p>;
  return <section className="topology-map-diagram" aria-label="Group topology diagram">
    <div className="topology-map-legend">
      <span>Source → replica</span><span>Solid: verified</span><span>Dashed: address match, not verified</span>
      <span>Dotted: same server (alias)</span>
    </div>
    <div className="topology-map-scroll" role="region" aria-label="Topology diagram, scroll to explore" tabIndex={0}>
      <div className="topology-map-canvas" style={{ width: diagram.width, height: diagram.height }}>
        {diagram.headings.map((heading) => <span key={heading.y} className="topology-map-tier" style={{ top: heading.y }}>{heading.label}</span>)}
        {diagram.blocks.map((block) => <div key={block.id} className={block.cluster ? "topology-map-cluster" : "topology-map-block"}
          role={block.cluster ? "group" : undefined} aria-label={block.cluster ? `${CLUSTER[block.cluster.kind]}: ${block.cluster.name}` : undefined}
          data-cluster={block.cluster?.id} style={{ left: block.x, top: block.y, width: block.width, height: block.height }}>
          {block.cluster && <div className="topology-map-cluster-heading" title={`${CLUSTER[block.cluster.kind]}: ${block.cluster.name}`}>
            <strong>{CLUSTER[block.cluster.kind]}</strong><span>{block.cluster.name}</span>
          </div>}
          {block.cluster && !block.nodes.length && <span className="topology-map-cluster-empty">No confirmed members</span>}
          {block.nodes.map(({ node, x, y }) => {
            const peer = node.kind === "peer";
            const key = peer ? node.id : node.profile_id;
            const aliasOf = !peer && node.duplicate_of
              ? (map.nodes.find((other) => other.id === node.duplicate_of)?.label || node.duplicate_of)
              : null;
            const tone = peer ? "unknown" : node.outcome === "failed" ? "problem" : node.outcome === "not_reached" ? "watch" : stateTone(node.state);
            const status = peer ? "Not in this group" : node.outcome !== "read" ? OUTCOME[node.outcome] : node.state || "State not reported";
            const engine = peer ? "Reported peer" : [node.product || (node.engine === "mariadb" ? "MariaDB" : node.engine === "mysql" ? "MySQL" : "Server"), node.version].filter(Boolean).join(" ");
            return <button key={node.id} type="button" className={`topology-map-node tone-${tone}${peer ? " is-peer" : ""}${selection.key === key ? " is-selected" : ""}`}
              data-node={node.id} data-row-key={key} data-outcome={peer ? undefined : node.outcome}
              aria-pressed={selection.key === key} aria-label={[node.label, mapAddress(node), engine, !peer && node.role ? ROLE[node.role] || node.role : "", aliasOf ? `Alias of ${aliasOf}` : "", status].filter(Boolean).join(", ")}
              title={`${node.label}\n${mapAddress(node)}\n${engine}${aliasOf ? `\nAlias of ${aliasOf}` : ""}\n${status}`}
              style={{ left: x, top: y, width: CARD_W, height: CARD_H }} onClick={() => selection.select(key, true)}>
              <span className="topology-eyebrow"><ServerGlyph /><span>{engine}</span></span>
              <strong className="topology-map-node-name">{node.label}</strong>
              <span className="topology-map-node-address">{mapAddress(node)}</span>
              {aliasOf && <span className="topology-map-alias">Alias of {aliasOf}</span>}
              <span className="topology-map-node-state">{!peer && node.role ? `${ROLE[node.role] || node.role} · ` : ""}{status}</span>
            </button>;
          })}
        </div>)}
        <svg className="topology-map-edges" width={diagram.width} height={diagram.height} viewBox={`0 0 ${diagram.width} ${diagram.height}`} aria-label="Topology links">
          <defs>{["ok", "watch", "problem", "unknown"].map((tone) => <marker key={tone} id={`${arrow}-${tone}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0 0 10 5 0 10Z" fill={`var(--${tone === "watch" ? "warn" : tone === "problem" ? "danger" : tone === "unknown" ? "muted" : "ok"})`} />
          </marker>)}</defs>
          {map.edges.map((edge, i) => {
            const from = diagram.positions.get(edge.from);
            const to = diagram.positions.get(edge.to);
            if (!from || !to) return null;
            const sourceBlock = diagram.blocks.find((block) => block.nodes.some(({ node }) => node.id === edge.from))!;
            const targetBlock = diagram.blocks.find((block) => block.nodes.some(({ node }) => node.id === edge.to))!;
            const parallel = map.edges.filter((other) => other.from === edge.from && other.to === edge.to);
            const slot = parallel.indexOf(edge) - (parallel.length - 1) / 2;
            const line = edgePath(from, to, i % 4, slot, diagram.width - 76 + (i % 5) * 12, sourceBlock, targetBlock);
            const title = `${map.nodes.find((node) => node.id === edge.from)?.label || edge.from} → ${map.nodes.find((node) => node.id === edge.to)?.label || edge.to}: ${mapEdgeText(edge)}`;
            return <g key={`${edge.from}-${edge.to}-${edge.kind}-${i}`} className={`topology-map-edge tone-${stateTone(edge.state)}${edge.kind === "alias" ? " is-alias" : ""}`}
              data-edge-kind={edge.kind} data-verified={String(edge.verified)}>
              <title>{title}</title>
              <path d={line.d} fill="none" stroke="currentColor" strokeWidth="2"
                strokeDasharray={edge.kind === "alias" ? "2 4" : edge.verified ? undefined : "6 5"} markerEnd={`url(#${arrow}-${stateTone(edge.state)})`} />
              <path className="topology-map-edge-hit" d={line.d} fill="none" stroke="transparent" strokeWidth="16" />
              <text x={line.x} y={line.y - 9} textAnchor="middle">{mapEdgeLabel(edge)}{edge.kind === "alias" ? "" : edge.state ? ` · ${edge.state}` : " · State not reported"}</text>
            </g>;
          })}
        </svg>
      </div>
    </div>
  </section>;
}
