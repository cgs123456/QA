# ScreenCaptureKit 文档预研（P10/F2.3，macOS 回环采集候选）

> 性质：**纯文档预研，不写码**。以下“确认”指 Apple 官方文档原文可查（2026-09-15 核）；
> “待真机”指必须在 macOS 真机 + Tauri 壳里实测才能落定的项，见 §4 清单。
> 背景：本项目 macOS 走“仅麦克风”正是因为**无系统回环采集**
> （见 PROGRESS「采集路径矩阵」）；若 ScreenCaptureKit 可行，macOS 可补回环路。

## 1. API 能力（确认，Apple Developer 文档）

- 框架基线 **macOS 12.3+**（`SCStream` / `SCContentFilter` /
  `SCStreamConfiguration` / `SCShareableContent`）。
- 采集对象三选一：display（`SCDisplay`）、app（`SCRunningApplication`）、
  window（`SCWindow`），经 `SCContentFilter` 组合（含 `excludingWindows`）。
- 输出：`SCStreamOutput` delegate 按 `SCStreamOutputType`（Screen / Audio 等）
  回调 `CMSampleBuffer`（视频帧 + 音频采样，带 `SCStreamFrameInfo` 元数据）。
- 流可运行时重配：`updateConfiguration` / `updateContentFilter`
 （含 completionHandler）；另有 `SCScreenshotManager` 单帧接口与系统
  `SCContentSharingPicker`（用户选源 UI）。
- Rust 生态有现成绑定 crate `screencapturekit`（docs.rs v7，含 async API 与
  23 个可运行 example；是否引入待 Phase 4 评估体积/维护，不在本轮结论内）。

## 2. 已知限制（确认，Chromium/WebRTC 源码注释 + 官方文档）

- **12.x 全屏采集不可用**：Chromium 注释 `IsScreenCaptureKitDesktopWorking`——
  macOS < 13 回退旧路；WebRTC 注释：12.3 有 API 但全屏 capture broken（crbug 40234870）。
  → 本项目若用，**最低门槛 macOS 13**（13 以下沿用“仅麦克风”，不分支旧 API）。
- `contentRect` / `pointPixelScale`（窗口区域裁剪）要 **macOS 14+**（WebRTC 以
  `API_AVAILABLE(macos(14.0))` 门控）；我们的提词场景若只要全窗/全屏可忽略，
  若要区域裁剪则门槛再抬到 14。
- Chromium 只接了 `TYPE_SCREEN` / `TYPE_WINDOW`（他们没接 app 级）——
  说明 app 级过滤的坑比文档多，真机清单里单列。
- 音频路存在但版本行为待核（见 §4）：只承诺“有 Audio 输出类型”，
  不承诺各版本语义一致。

## 3. 对本项目的适配判断（预研结论，非承诺）

- 回环等价物 = **display 级 Screen + Audio 双输出**，PCM 进现有
  `CapturePipeline`（16k 重采样链路复用，格式转换在 Rust 侧做，不碰 sidecar）。
- 权限面：Screen Recording 权限必弹系统框（用户拒绝 = 该路永久不可用，
  前端按现有“采集不可用”降级面处理，不新增错误种类）。
- 不确定性全部收敛到 §4 清单；任一条不满足就维持“macOS 仅麦克风”，
  **不为 SCK 改 sidecar 协议**（`audio/uplink.rs` 的帧契约不动）。

## 4. Phase 4 真机清单（macOS 实机 + Tauri 壳，逐条打勾）

1. [ ] macOS 13/14/15 三档：display 级 Screen+Audio 流能建连、出首帧（ rationalize 最低门槛）。
2. [ ] 音频语义：采样率/声道/位深实测值；与系统音量/静音键的关系；有无回声/增益处理。
3. [ ] 权限拒绝路径：UI 降级提示出现，不崩不卡；重新授权后无需重启恢复。
4. [ ]  muted/最小化/全屏 app、窗口 resize、中途切 display：流不断或按契约重建
   （对标现有 `DeviceChanged` + `StreamRebuilt` 语义）。
5. [ ] 连续 10 分钟 CPU/内存（与现有 bench 口径对齐：segment 级时延 + RSS 增量）。
6. [ ] 多显示器：主副屏切换、副屏断开。
7. [ ] app 级过滤是否可用（Chromium 没接的原因是否影响我们；用不上就写死 display 级）。
8. [ ] Rust 绑定选型（直调 objc vs `screencapturekit` crate）：体积增量 + 最低版本门控写法。
