import { InfoPopover, ToolbarButton } from "./kit";
import "./topology-presentation.css";

/** Presentation controls never change a server or imply a measured rate. */
export function TopologyControls({ motion, depth, onMotion, onDepth }: {
  motion: boolean; depth: boolean; onMotion: () => void; onDepth: () => void;
}) {
  return <div className="topology-presentation-controls" role="group" aria-label="Topology appearance">
    <ToolbarButton className="small" aria-pressed={depth} onClick={onDepth}>Depth</ToolbarButton>
    <ToolbarButton className="small topology-motion-toggle" aria-pressed={motion} onClick={onMotion}>
      {motion ? "Pause motion" : "Resume motion"}
    </ToolbarButton>
    <InfoPopover label="About topology motion">Motion illustrates the last reported running state or healthy group membership. It does not measure traffic or throughput. Stopped, failed and unknown links stay still. Your system’s reduced motion preference takes priority.</InfoPopover>
  </div>;
}

/** A compact isometric server silhouette adds depth without rotating labels. */
export function ServerGlyph() {
  return <svg className="topology-server-glyph" viewBox="0 0 32 32" aria-hidden="true">
    <path className="server-top" d="M4 7 16 2 28 7 16 12Z" />
    <path className="server-side" d="M16 12 28 7v18L16 30Z" />
    <path className="server-front" d="M4 7 16 12v18L4 25Z" />
    <path className="server-slots" d="m7 13 6 2.5m-6 3 6 2.5m-6 3 6 2.5" />
  </svg>;
}
