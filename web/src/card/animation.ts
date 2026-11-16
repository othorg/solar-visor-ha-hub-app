// Energy-ball motion – port of SolarVisorKit/FlowAnimation.swift (speed curve, hysteresis gate, direction machine) and
// the app's FlowAnimator (DashboardView.swift). Keep in step with Swift – the shared fixtures check both.
import { BallColours, wires as allocate, inputsFromSnapshot, type Shares, type Source, type WireFlow } from "./allocation";
import type { PlantSnapshot } from "./model";
import type { Topology } from "./topology";

export interface SpeedParameters {
  onThresholdW: number;
  offThresholdW: number;
  saturationW: number;
  minSpeed: number;
  maxSpeed: number;
  exponent: number;
}

export const SPEED: SpeedParameters = {
  onThresholdW: 50, offThresholdW: 30, saturationW: 15_000, minSpeed: 8, maxSpeed: 180, exponent: 0.6,
};

/** Monotone mapping |P| → pt/s on the reference raster; an active edge below the on-threshold creeps at minSpeed. */
export function speed(powerW: number, isActive = false, p: SpeedParameters = SPEED): number {
  const magnitude = Math.min(Math.abs(powerW), p.saturationW);
  if (magnitude < p.onThresholdW) return isActive ? p.minSpeed : 0;
  const x = (magnitude - p.onThresholdW) / (p.saturationW - p.onThresholdW);
  const v = p.minSpeed + (p.maxSpeed - p.minSpeed) * Math.pow(x, Math.max(0.1, p.exponent));
  return Math.min(Math.max(v, p.minSpeed), p.maxSpeed);
}

/** Hysteresis gate against flicker at small powers: on ≥ 50 W, off < 30 W. */
export class ActivityGate {
  isActive = false;

  constructor(private readonly p: SpeedParameters = SPEED) {}

  update(powerW: number | null): boolean {
    if (powerW === null) {
      this.isActive = false;
      return false;
    }
    const a = Math.abs(powerW);
    if (this.isActive) {
      if (a < this.p.offThresholdW) this.isActive = false;
    } else if (a >= this.p.onThresholdW) {
      this.isActive = true;
    }
    return this.isActive;
  }
}

export type DirectionPhase =
  | { kind: "idle" }
  | { kind: "running"; forward: boolean }
  | { kind: "fadingOut"; toForward: boolean; elapsed: number }
  | { kind: "fadingIn"; forward: boolean; elapsed: number };

export const FADE = 0.3;

/** Direction change: fade out (0.3 s) → reverse → fade in (0.3 s); never counter-running balls on one edge. */
export class DirectionMachine {
  phase: DirectionPhase = { kind: "idle" };

  /** Opacity 0…1 of the edge's balls; `forward` null = no flow / unknown / stale. */
  step(forward: boolean | null, dt: number): number {
    const ph = this.phase;
    switch (ph.kind) {
      case "idle":
        if (forward !== null) this.phase = { kind: "fadingIn", forward, elapsed: 0 };
        return 0;
      case "running":
        if (forward === null) {
          this.phase = { kind: "idle" };
          return 0;
        }
        if (forward !== ph.forward) {
          this.phase = { kind: "fadingOut", toForward: forward, elapsed: 0 };
          return 1;
        }
        return 1;
      case "fadingOut": {
        const t = ph.elapsed + dt;
        if (t >= FADE) {
          this.phase = forward !== null ? { kind: "fadingIn", forward, elapsed: 0 } : { kind: "idle" };
          return 0;
        }
        this.phase = { kind: "fadingOut", toForward: forward ?? ph.toForward, elapsed: t };
        return 1 - t / FADE;
      }
      case "fadingIn": {
        if (forward === null) {
          this.phase = { kind: "idle" };
          return 0;
        }
        if (forward !== ph.forward) {
          // continue from the current opacity instead of jumping back to fully visible
          this.phase = { kind: "fadingOut", toForward: forward, elapsed: Math.max(0, FADE - ph.elapsed) };
          return Math.min(1, ph.elapsed / FADE);
        }
        const t = ph.elapsed + dt;
        if (t >= FADE) {
          this.phase = { kind: "running", forward: ph.forward };
          return 1;
        }
        this.phase = { kind: "fadingIn", forward: ph.forward, elapsed: t };
        return t / FADE;
      }
    }
  }

  get currentForward(): boolean | null {
    const ph = this.phase;
    return ph.kind === "running" || ph.kind === "fadingIn" ? ph.forward : null;
  }
}

export interface EdgeState {
  gate: ActivityGate;
  direction: DirectionMachine;
  speed: number;          // smoothed pt/s on the reference raster
  phase: number;          // accumulated travel distance (reference raster)
  opacity: number;
  renderForward: boolean;
  shares: Shares;
  colours: BallColours;
}

const SMOOTHING_S = 1.5;  // τ (UI spec §2.4)

/** The flow model of a snapshot; nothing while the data is stale. */
export function flowWires(snapshot: PlantSnapshot | null, topology: Topology, stale: boolean): Record<string, WireFlow> {
  return stale ? {} : allocate(inputsFromSnapshot(snapshot, topology));
}

/** One state per wire: gate, direction, smoothed speed, travelled distance and the colours of its balls. */
export class FlowAnimator {
  private readonly edges = new Map<string, EdgeState>();
  private last: number | null = null;
  private cached: { snapshotId: string | null; revision: number; stale: boolean; wires: Record<string, WireFlow> } | null = null;

  /** `now` in seconds. */
  advance(now: number, snapshot: PlantSnapshot | null, topology: Topology, ids: string[], stale: boolean): void {
    const dt = Math.min(Math.max(now - (this.last ?? now), 0), 0.1);
    this.last = now;
    const c = this.cached;
    if (!c || c.snapshotId !== (snapshot?.snapshot_id ?? null) || c.stale !== stale || c.revision !== topology.revision) {
      if (c && c.revision !== topology.revision) this.edges.clear();      // another plant: no old wire state
      this.cached = { snapshotId: snapshot?.snapshot_id ?? null, revision: topology.revision, stale,
                      wires: flowWires(snapshot, topology, stale) };
    }
    const wires = this.cached?.wires ?? {};
    for (const id of ids) {
      let st = this.edges.get(id);
      if (!st) {
        st = { gate: new ActivityGate(), direction: new DirectionMachine(), speed: 0, phase: 0, opacity: 0,
               renderForward: true, shares: {}, colours: new BallColours() };
        this.edges.set(id, st);
      }
      const flow = wires[id];
      const power = flow ? flow.powerW : null;
      const active = st.gate.update(power);
      const forward = active ? (power ?? 0) >= 0 : null;
      st.opacity = st.direction.step(forward, dt);
      const current = st.direction.currentForward;
      if (current !== null && current !== st.renderForward) {
        st.renderForward = current;
        st.colours = new BallColours();      // a reversed wire carries another source – a new stream
      }
      if (forward === st.renderForward) st.shares = flow?.shares ?? {};
      if (st.opacity < 0.01) {
        st.colours = new BallColours();
        st.shares = {};
      }
      const target = active ? speed(power ?? 0, true) : 0;
      st.speed += (target - st.speed) * (1 - Math.exp(-dt / SMOOTHING_S));
      st.phase += st.speed * dt;
    }
  }

  state(id: string): EdgeState | undefined {
    return this.edges.get(id);
  }

  colour(id: string, identity: number): Source | null {
    const st = this.edges.get(id);
    return st ? st.colours.colour(identity, st.shares) : null;
  }

  forget(id: string, except: Set<number>): void {
    this.edges.get(id)?.colours.keepOnly(except);
  }
}
