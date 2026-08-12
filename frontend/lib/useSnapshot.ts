"use client";

import { useEffect, useRef, useState } from "react";
import { fetchSnapshot, POLL_MS } from "./api";
import type { SnapshotState } from "./types";

/** Polls the read-only backend snapshot. On any failure the state is `connected:false` with
 *  `data:null` → the UI shows NO DATA everywhere (never fabricated). */
export function useSnapshot(): SnapshotState {
  const [state, setState] = useState<SnapshotState>({
    data: null, loading: true, connected: false, error: null, lastFetch: null,
  });
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function tick() {
      try {
        const data = await fetchSnapshot(controller.signal);
        if (cancelled) return;
        setState({ data, loading: false, connected: true, error: null,
          lastFetch: new Date().toISOString() });
      } catch (e: any) {
        if (cancelled) return;
        setState((s) => ({
          data: null, loading: false, connected: false,
          error: e?.message === "NO_BACKEND" ? "no backend configured" : (e?.message ?? "unreachable"),
          lastFetch: s.lastFetch,
        }));
      } finally {
        if (!cancelled) timer.current = setTimeout(tick, POLL_MS);
      }
    }
    tick();
    return () => {
      cancelled = true;
      controller.abort();
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);

  return state;
}
