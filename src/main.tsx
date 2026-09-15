import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { TeleprompterWindow } from "./pages/TeleprompterWindow";
import { readWindowView } from "./lib/windowView";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

// taskP7：提词窗是同一个前端入口在另一个 WebviewWindow 里跑，靠 Rust 注入的
// 全局标记分叉（见 src/lib/windowView.ts）。默认（认不出）走主窗。
const view = readWindowView(window as unknown as Record<string, unknown>);

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      {view === "teleprompter" ? <TeleprompterWindow /> : <App />}
    </QueryClientProvider>
  </React.StrictMode>,
);
