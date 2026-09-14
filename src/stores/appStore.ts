import { create } from "zustand";

export type SidecarStatus = {
  connected: boolean;
  port: number | null;
};

export type DegradedInfo = {
  reason: string;
  detail: string;
};

type AppState = {
  status: SidecarStatus | null;
  degraded: DegradedInfo | null;
  error: string | null;
  setStatus: (s: SidecarStatus | null) => void;
  setDegraded: (d: DegradedInfo | null) => void;
  setError: (e: string | null) => void;
};

export const useAppStore = create<AppState>((set) => ({
  status: null,
  degraded: null,
  error: null,
  setStatus: (status) => set({ status }),
  setDegraded: (degraded) => set({ degraded }),
  setError: (error) => set({ error }),
}));
