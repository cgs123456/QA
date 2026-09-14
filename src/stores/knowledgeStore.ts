import { create } from "zustand";

export type StoreInfo = {
  id: string;
  name: string;
  template_id: string | null;
  created_at: string;
  is_current: boolean;
  qa_count: number;
  field_count: number;
};

type KnowledgeState = {
  stores: StoreInfo[];
  /** 当前选中库（默认跟随服务端的 is_current）。 */
  selectedId: string | null;
  setStores: (s: StoreInfo[]) => void;
  setSelectedId: (id: string | null) => void;
};

export const useKnowledgeStore = create<KnowledgeState>((set) => ({
  stores: [],
  selectedId: null,
  setStores: (stores) => set({ stores }),
  setSelectedId: (selectedId) => set({ selectedId }),
}));
