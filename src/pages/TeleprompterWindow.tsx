import { useEffect, useState } from "react";
import { listen } from "@tauri-apps/api/event";

import { Teleprompter } from "../components/Teleprompter";
import { EVT_TELEPROMPTER_CARDS } from "../hooks/useLiveQA";
import type { LiveCard } from "../lib/liveqa";

/**
 * 提词窗（taskP7）：独立 `WebviewWindow` 里的内容面。
 *
 * # 为什么它**不**自己跑一份 `useLiveQA`
 *
 * 提词窗是主窗「实时提词」页的**镜像**，不是第二个会话。两个窗各跑一份会话的话，
 * 同一个问题会往 sidecar 发两次检索（一次提问、两份 LLM 调用），而且两边的卡片
 * 会因为各自去重状态不同而漂移。
 *
 * 所以：主窗（`LiveQA` 页，`broadcast: true`）把**已经算好的**卡片 emit 成
 * `teleprompter://cards`，提词窗只订阅、只渲染。主窗被收进托盘时 React 仍然挂载，
 * 会话照跑 —— 这正是「收托盘 + 浮提词窗」的用法。
 *
 * # 已知边界（写在代码里，不靠记性）
 *
 * 主窗没停在「实时提词」页时没有会话，提词窗就是空的。这是刻意的：
 * 「什么时候响、响什么」的策略只在一处（`lib/liveqa.ts`），不给提词窗开第二条路径。
 */
export function TeleprompterWindow() {
  const [cards, setCards] = useState<readonly LiveCard[]>([]);

  useEffect(() => {
    // 窗口本身是 transparent 的，页面背景必须跟着透明，
    // 否则透明窗口里还是一块 `:root` 的浅灰（见 App.css 的 .overlay-window）。
    document.documentElement.classList.add("overlay-window");
    return () => document.documentElement.classList.remove("overlay-window");
  }, []);

  useEffect(() => {
    let cancelled = false;
    let unlisten: (() => void) | undefined;
    listen<LiveCard[]>(EVT_TELEPROMPTER_CARDS, (event) => {
      if (!cancelled) setCards(event.payload ?? []);
    })
      .then((fn) => {
        if (cancelled) fn();
        else unlisten = fn;
      })
      .catch(() => {
        // 非 Tauri 环境（vitest / 浏览器直开）没有事件总线：保持空态，不报错。
      });
    return () => {
      cancelled = true;
      unlisten?.();
    };
  }, []);

  return (
    <div className="teleprompter-window" data-testid="teleprompter-window">
      <Teleprompter cards={cards} />
    </div>
  );
}
