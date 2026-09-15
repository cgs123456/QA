# taskP7 壳内人工验证清单

> **本清单由用户在本机执行**（自动化测试碰不到真窗口、真托盘、真屏幕捕获）。
> 逻辑层的决策已经由 `cargo test` 钉住（`stealth::overlay` / `tray` / `autostart`
> 三个模块的 `mod tests`）；这里只验证**需要真壳才能看见**的部分。
>
> 执行完请在文末「结论」表里逐项填 `通过 / 不通过 / 未执行` 与备注。

## 0. 前置

起壳（Git Bash）：

```bash
export PATH="/c/Users/Administrator/.cargo/bin:$PATH"
export NO_PROXY="127.0.0.1,localhost"
export CARGO_INCREMENTAL=0
export INTERVIEWCOPILOT_PYTHON="C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
cd /c/Users/Administrator/Desktop/QA && pnpm tauri dev
```

**注意**：主窗的「关闭」按钮现在会**收进托盘**而不是退出（taskP7 的行为）。
退出请用托盘右键 →「退出」。

### 在 devtools 里调命令

本项目的 `tauri.conf.json` **没有**开 `withGlobalTauri`，所以 `window.__TAURI__` 不存在。
在 devtools（debug 构建默认可用）的 Console 里用注入的内部对象：

```js
await window.__TAURI_INTERNALS__.invoke("get_overlay_status")
```

若该对象也不存在（Tauri 版本差异），临时给 `tauri.conf.json` 的 `app` 加
`"withGlobalTauri": true` 重启，改用 `await window.__TAURI__.core.invoke("get_overlay_status")`，
验证完请还原。

---

## A. 提词窗外观（`stealth::overlay::spec`）

| # | 步骤 | 期望 |
|---|---|---|
| A1 | 主窗停在「实时提词」页，托盘右键 →「显示/隐藏提词窗」 | 出现一个**无边框、无标题栏**的小窗（420×320） |
| A2 | 看窗口区域 | **透明**：能看见它背后的桌面/其它窗口内容，没有浅灰底 |
| A3 | 切到别的应用（浏览器/记事本），把它拖到提词窗上方 | 提词窗**始终在最上层** |
| A4 | 按 `Alt+Tab` | 任务切换列表里**看不到**提词窗（`skip_taskbar`） |
| A5 | 先在记事本里打字，然后让提词窗显示 | 焦点**仍在记事本**，打字不断（`focusable=false`）；这是本功能最关键的预期 |
| A6 | 主窗「实时提词」页开始采集并制造一次提问（或点「手动提词」） | 提词窗里出现**与主窗相同的卡片**（同一个问题的同一份答案） |

> A6 若为空：先确认主窗停在「实时提词」页（提词窗是那一页的镜像，
> 没有会话时它就是空的 —— 见 `src/pages/TeleprompterWindow.tsx` 头注释）。

---

## B. 屏幕捕获排除（Windows，`WDA_EXCLUDEFROMCAPTURE`）

**B 组是 taskP7 的核心验收项，请务必做 B4 的对照实验。**

| # | 步骤 | 期望 |
|---|---|---|
| B1 | 提词窗可见时，按 `Win+Shift+S` 框选包含提词窗的区域 | 截到的图里**没有**提词窗（该区域是它背后的内容） |
| B2 | 用另一个截图/录屏工具（QQ 截图、OBS 窗口捕获之外的显示器捕获）重做 B1 | 同上 |
| B3 | **同时**盯着屏幕看提词窗 | **本人照常看得见**。这不是 bug，是设计：`WDA_*` 只影响捕获。见 `docs/stealth-boundary.md` §4 |
| B4 | **对照实验**：临时把 `src-tauri/src/stealth/overlay.rs` 里 `apply_capture_exclusion` 的函数体改成直接 `return CaptureExclusion::Unsupported;`，重编译，重做 B1 | 这次截图里**能看见**提词窗 → 证明 B1 是 WDA 在起作用，而不是「窗口透明所以看不见」 |
| B5 | 恢复 B4 的改动，在 devtools 里 `await window.__TAURI_INTERNALS__.invoke("get_overlay_status")` | 返回 `{"label":"teleprompter","present":true,"visible":true,"capture_exclusion":"applied"}` |
| B6 | 用 OBS「显示器捕获」录 10 秒（提词窗开着） | 回放里提词窗不可见 |

> B4 是防自欺的一步：一个全透明窗口本来就「拍不到内容」，只有对照过才知道
> 到底是 WDA 生效，还是我们测了个假命题。

---

## C. 生命周期：创建 / 销毁 / 重建

| # | 步骤 | 期望 |
|---|---|---|
| C1 | 托盘「显示/隐藏提词窗」连点 4 次 | 显→隐→显→隐，无闪烁、无「建完立刻隐藏」的闪一下 |
| C2 | devtools 执行 `await window.__TAURI_INTERNALS__.invoke("destroy_overlay")` | 提词窗消失；`get_overlay_status` 返回 `present:false` |
| C3 | 再执行 `await window.__TAURI_INTERNALS__.invoke("set_overlay", { visible: true })` | 窗口**重建**，A1–A5 的属性全部还在（尤其是 B5 的 `applied`） |
| C4 | C2 之后看主窗 | 主应用**完全不受影响**（不崩、不卡、sidecar 还在） |
| C5 | C2 之后看 stderr | 出现一行 `[stealth] 提词窗已销毁（重建入口：…）` |

---

## D. 托盘（`tray.rs`）

| # | 步骤 | 期望 |
|---|---|---|
| D1 | 应用启动后看系统托盘 | 出现应用图标（复用 `icons/`，未额外引入托盘资源） |
| D2 | **左键单击**托盘图标 | 主窗隐藏；再单击 → 显示并聚焦 |
| D3 | 右键单击托盘图标 | 三项菜单：「显示/隐藏提词窗」「开始/停止采集」「退出」 |
| D4 | 看「开始/停止采集」 | **灰色不可点**（`CAPTURE_WIRED = false`，taskP7 刻意灰置） |
| D5 | 点「退出」 | 进程真的退出；任务管理器里**没有残留的 python sidecar**（R7） |
| D6 | 重开应用，点主窗的「关闭」按钮 | 主窗**收进托盘**、进程仍在（托盘图标还在） |
| D7 | 再点托盘图标 | 主窗重新出现 |
| D8 | **失败路径**：把 `src-tauri/src/tray.rs::setup` 开头改成 `return Err("test".into());`，重编译，点主窗「关闭」 | 应用**直接退出**（不是隐藏）。理由：托盘不可用时若还隐藏，用户就再也关不掉这个程序了（`tray::close_plan`）。验证完请还原 |

> **D2 若没反应或「切两次」**：`tauri::tray::MouseButtonState` 的 doc 注释把
> `Up`/`Down` 写反了（`Up` 的注释写着 "pressed"）。当前代码认 `Up`（松开）。
> 若本机行为相反，把 `tray.rs` 里 `on_tray_icon_event` 的
> `MouseButtonState::Up` 改成 `MouseButtonState::Down` 即可 —— 这是唯一一处。

---

## E. 开机自启（`autostart.rs`）

| # | 步骤 | 期望 |
|---|---|---|
| E1 | Settings 页看「开机自启」 | 复选框默认**未勾选**，下面一行「未设置开机自启。」 |
| E2 | 勾选 | 提示变「已设置开机自启。」 |
| E3 | 打开注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` | 出现 `interview-copilot` 项，值为 exe 路径 |
| E4 | 取消勾选 | 提示变「未设置开机自启。」；E3 的注册表项消失 |
| E5 | **失败路径**：重新勾选后，用管理员把 `HKCU\...\Run` 的写权限收回（或组策略禁用 Run 项），再取消勾选 | 提示显示失败原因，**应用其余功能正常**（不是崩溃、不是静默失败） |
| E6 | （可选，需重启）E2 之后注销再登录 | 应用自动启动 |

---

## F. 本轮明确未做（不是缺陷，是范围）

| 项 | 状态 | 说明 |
|---|---|---|
| 提词窗拖动/定位 | 未做 | `focusable=false` 的代价。鼠标消息仍能收到，后续可加 `data-tauri-drag-region` |
| 托盘菜单标签动态化 | 未做 | 标签固定为「显示/隐藏提词窗」；动态 `set_text` 要跨窗口持有 `MenuItem` 句柄，不划算 |
| 提词窗内交互（滚动/复制） | 未做 | 当前只渲染 |
| 主窗不在「实时提词」页时的提词内容 | 设计如此 | 提词窗是那一页的镜像；策略只在一处（`lib/liveqa.ts`） |
| macOS `sharingType(.none)` | 代码就位，未验证 | 本机无 mac。见 `docs/stealth-boundary.md` §4 |
| Linux 捕获排除 | 平台不支持 | 如实上报 `best_effort`，不做偏门手段 |

---

## 结论（执行后填写）

| 组 | 结论 | 备注 |
|---|---|---|
| A 外观 | 未执行 | |
| B 捕获排除 | 未执行 | |
| C 生命周期 | 未执行 | |
| D 托盘 | 未执行 | |
| E 开机自启 | 未执行 | |

**执行人**：　　　　**日期**：　　　　**构建**：`git rev-parse --short HEAD` = 　　　
