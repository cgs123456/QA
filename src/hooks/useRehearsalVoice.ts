/**
 * 陪练页的「口述作答」：复用**既有采集链**，不造第二套音频生命周期（S5 裁定 ⑦）。
 *
 * 与实时提词的区别：提词页订阅转写是为了**触发检索**；这里只把最新转写塞进
 * 作答框，**不发任何检索请求**（Rehearsal 全程不 import `askQuestion`）。
 *
 * 为什么单独成 hook：页面测试的约定是「mock 掉 hook 层，测页面怎么画」
 * （见 `History.test.tsx` 头注）。把 Tauri 的 `listen` / `setCapture` 关在这里，
 * 页面测试就不必去 mock `@tauri-apps/api/event`。
 *
 * 采集不可用（无设备 / 无权限 / 不在 Tauri 壳里）只影响口述：键入照常，
 * 错误就地显示，**不阻断练习**。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { setCapture } from "../lib/capture";
import { EVT_ASR_FINAL } from "./useLiveQA";

export type UseRehearsalVoiceOptions = {
  /** 收到一段转写时回调（页面把它并进作答框）。 */
  onTranscript: (text: string) => void;
};

export type RehearsalVoice = {
  /** 采集开着（口述中）。 */
  on: boolean;
  /** 正在开/关（按钮防连点）。 */
  busy: boolean;
  /** 就地错误：无设备 / 无权限 / 不在壳里。null = 无错。 */
  error: string | null;
  toggle: () => Promise<void>;
};

export function useRehearsalVoice({
  onTranscript,
}: UseRehearsalVoiceOptions): RehearsalVoice {
  const [on, setOn] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const unlistenRef = useRef<(() => void) | null>(null);
  // 回调放 ref：订阅只建一次，避免 onTranscript 每次 render 变化导致重复订阅。
  const cbRef = useRef(onTranscript);
  cbRef.current = onTranscript;

  const teardown = useCallback(() => {
    unlistenRef.current?.();
    unlistenRef.current = null;
  }, []);

  // 组件卸载必须退订并停采集 —— 否则离开页面后麦克风还开着。
  useEffect(() => {
    return () => {
      teardown();
    };
  }, [teardown]);

  const toggle = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      if (on) {
        teardown();
        await setCapture(false);
        setOn(false);
        return;
      }
      await setCapture(true);
      // 动态 import：不在 Tauri 壳里时这一步才失败，且失败只影响口述。
      const { listen } = await import("@tauri-apps/api/event");
      const unlisten = await listen<{ text?: string }>(EVT_ASR_FINAL, (event) => {
        const text = event.payload?.text?.trim();
        if (text) cbRef.current(text);
      });
      unlistenRef.current = unlisten;
      setOn(true);
    } catch (e) {
      // 失败要把已开的采集收回去，不能留下半开状态。
      try {
        await setCapture(false);
      } catch {
        /* 收不回来也无更坏的结果：状态按未开处理 */
      }
      teardown();
      setOn(false);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [on, teardown]);

  return { on, busy, error, toggle };
}
