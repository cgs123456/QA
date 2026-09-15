/**
 * 采集服务的前端接线（M2-7）：命令封装 + `capture://state` 的渲染逻辑。
 *
 * 与 `lib/liveqa.ts` 同样的分工：**判断都在这里（可脱离运行时单测），
 * 组件只负责把结果画出来**。所以「自检通过没有」「为什么失败」都是纯函数，
 * 不需要起 Tauri 就能测。
 *
 * Rust 侧对应：`src-tauri/src/commands/audio.rs`、`src-tauri/src/audio/service.rs`。
 *
 * # 类型对齐的一条约定
 *
 * Rust 快照里的 `device: String` / `native_sample_rate: u32` 等**不是 `Option`**
 * —— 快照每 100ms 被读一次，不为了「未知」多付一次判空。所以前端约定：
 * **`""` 与 `0` 表示未知**，由下面的格式化函数统一翻译，组件里不再散落 `if (x > 0)`。
 */

import { invoke } from "@tauri-apps/api/core";

import type { CapturePath } from "./liveqa";

/** Rust `commands::audio::EVT_CAPTURE_STATE`。状态回推事件（真值源）。 */
export const EVT_CAPTURE_STATE = "capture://state";

/** 与 Rust `PROBE_RATE_TOLERANCE` 同值：实测帧率允许偏离额定 ±35%。 */
export const PROBE_RATE_TOLERANCE = 0.35;

// ------------------------------------------------------------------ 类型（镜像 Rust 快照）

export type SelfCheckStatus = "pending" | "pass" | "fail";

/** 开流 500ms 探测的结果：格式 / 权限 / 实际帧率。 */
export type SelfCheck = {
  status: SelfCheckStatus;
  /** 失败原因（可读中文）；通过时为 null。 */
  reason: string | null;
  /** `""` = 未知（源还没报出设备名）。 */
  device: string;
  /** `0` = 未知。 */
  native_sample_rate: number;
  native_channels: number;
  /** 探测窗口内到达的帧数。 */
  observed_frames: number;
  /** 这些帧代表的音频时长（`frames × 30ms`）。 */
  observed_ms: number;
  expected_ms: number;
  /** 墙钟耗时。 */
  elapsed_ms: number;
  /** `0` = 未测出。 */
  measured_frame_rate: number;
  nominal_frame_rate: number;
};

export type EndpointStats = {
  frames: number;
  voiced_frames: number;
  segments_kept: number;
  segments_dropped: number;
  forced_cuts: number;
  holes: number;
  frames_lost: number;
};

export type UplinkStats = {
  frames_sent: number;
  events_sent: number;
  dropped: number;
  downlink_messages: number;
  unknown_downlink: number;
  malformed_downlink: number;
  close_code: number | null;
  closed: boolean;
  /** 成功重连次数（断线退避后接上的次数）。 */
  reconnects: number;
  /** 退避耗尽或被拒：这一路的上行已放弃（服务不会无限重试）。 */
  reconnect_exhausted: boolean;
  /** 已出队但发送失败的帧（会在线上留下可见的序号缺口）。 */
  send_failures: number;
};

/** 采集源自己的计数（直接来自 `LoopbackSource::stats()`）。 */
export type SourceStats = {
  frames_emitted: number;
  frames_dropped: number;
  /** 源侧错误（设备掉线等）；不含 VAD 分类失败。 */
  errors: number;
};

export type PathSnapshot = {
  path: CapturePath;
  running: boolean;
  /** 设备名（来自 `CaptureEvent::Started`）。 */
  device: string;
  /** 设备名（来自源自己的 `current_device()`）；源没报 `Started` 时是唯一非空的那个。 */
  source_device: string;
  /** 源侧计数（"已丢帧"的真实口径）。 */
  source_stats: SourceStats;
  native_sample_rate: number;
  native_channels: number;
  vad_provider: string;
  /** VAD 拒绝分类的帧数（生产应恒为 0）。 */
  vad_errors: number;
  last_vad_error: string | null;
  frames_emitted: number;
  frames_dropped: number;
  segments_sent: number;
  /** 因丢帧被迫封口的段数。 */
  holes: number;
  /** 候选段缓存溢出（构造上应为 0）。 */
  hold_overflow: number;
  push_errors: number;
  endpoint: EndpointStats;
  uplink: UplinkStats;
  self_check: SelfCheck;
  /** 该路不可用的原因（设备打不开 / worker 起不来）；正常时为 null。 */
  error: string | null;
};

export type CaptureState = {
  running: boolean;
  last_error: string | null;
  /**
   * 每路诊断。**在跑时是当前每路；没在跑时是上一次运行的终态** ——
   * 按完停止不该把面板清空（那时最该看的正是刚才那次跑得怎么样）。
   * 判断"现在有没有在采"只看 `running`，不要看 `paths` 是否为空。
   */
  paths: PathSnapshot[];
};

/** sidecar `/diagnostics/audio` 的 `capture` 段（它看得见的那部分采集状态）。 */
export type SidecarCapture = {
  status: string;
  /** 当前在连的 path（刚连上还没报 path 的连接不计）。 */
  active_paths?: string[];
  note: string;
};

/** 服务停着时的空状态：`get_capture_state` 未启动会返回它。 */
export const EMPTY_CAPTURE_STATE: CaptureState = { running: false, last_error: null, paths: [] };

// ------------------------------------------------------------------ 命令

export async function getCaptureState(): Promise<CaptureState> {
  return invoke<CaptureState>("get_capture_state");
}

export async function setCapture(enabled: boolean): Promise<CaptureState> {
  return invoke<CaptureState>("set_capture", { enabled });
}

export async function toggleCapture(): Promise<CaptureState> {
  return invoke<CaptureState>("toggle_capture");
}

// ------------------------------------------------------------------ 渲染逻辑（纯函数）

export const PATH_LABELS: Record<CapturePath, string> = {
  loopback: "系统回环",
  mic: "麦克风",
};

export function pathLabel(path: string): string {
  return PATH_LABELS[path as CapturePath] ?? path;
}

/** 原生格式一行，例如 `48000Hz / 2ch`；两者都未知时返回 null（不编造）。 */
export function formatNativeFormat(sc: SelfCheck): string | null {
  const rate = sc.native_sample_rate > 0 ? `${sc.native_sample_rate}Hz` : null;
  const ch = sc.native_channels > 0 ? `${sc.native_channels}ch` : null;
  if (rate == null && ch == null) return null;
  return `${rate ?? "?Hz"} / ${ch ?? "?ch"}`;
}

/**
 * 帧率是否落在容差内。
 *
 * 与 Rust 各算一遍是有意的：Rust 那边决定**判不判失败**，这里决定**怎么显示**。
 * 两者不一致时最多是显示语气不同，不会出现"UI 说好、服务说坏"。
 */
export function rateVerdict(
  measured: number | null | undefined,
  nominal: number,
  tolerance = PROBE_RATE_TOLERANCE,
): "unknown" | "ok" | "low" | "high" {
  if (measured == null || !Number.isFinite(measured) || measured <= 0) return "unknown";
  if (nominal <= 0) return "unknown";
  if (measured < nominal * (1 - tolerance)) return "low";
  if (measured > nominal * (1 + tolerance)) return "high";
  return "ok";
}

/**
 * 自检一行摘要 —— 诊断面板上用户唯一需要读的那句话。
 *
 * 通过时把「设备 + 格式 + 实测帧率」写全；失败时**直接引用 Rust 给的原因**，
 * 不在这里二次解释，避免两层措辞打架。
 */
export function selfCheckSummary(sc: SelfCheck): string {
  if (sc.status === "pending") return "自检中…（开流 500ms 探测）";
  if (sc.status === "fail") return `自检失败：${sc.reason ?? "原因未知"}`;

  const parts: string[] = [];
  if (sc.device !== "") parts.push(sc.device);
  const native = formatNativeFormat(sc);
  if (native != null) parts.push(native);
  if (sc.measured_frame_rate > 0) {
    parts.push(
      `实测 ${sc.measured_frame_rate.toFixed(1)} 帧/秒（额定 ${sc.nominal_frame_rate.toFixed(1)}）`,
    );
  }
  parts.push(`探测 ${sc.observed_frames} 帧 / ${sc.observed_ms}ms，用时 ${sc.elapsed_ms}ms`);
  return `自检通过：${parts.join(" · ")}`;
}

/** 一路的整体状态标签。优先级：错误 > 自检失败 > 未运行 > 运行中。 */
export function pathStatusLabel(p: PathSnapshot): string {
  if (p.error != null) return `不可用：${p.error}`;
  if (p.self_check.status === "fail") return "自检失败";
  if (!p.running) return "已停止";
  if (p.self_check.status === "pending") return "自检中";
  return "采集中";
}

/**
 * 服务级一行摘要（页面顶部用）。
 *
 * 刻意区分「在跑」与「跑得对不对」：`running` 为真但某路自检失败时，
 * 不能只说"运行中"——那正是用户以为在录音、实际什么都没送出去的场景。
 * 上行降级（退避耗尽 / 被拒）同理：音频还在采，但已经送不出去了。
 */
export function captureSummary(state: CaptureState): string {
  if (state.last_error != null) return `采集异常：${state.last_error}`;
  if (!state.running) return "未采集";
  const degraded = state.paths.filter((p) => p.uplink.reconnect_exhausted);
  if (degraded.length > 0) {
    return `采集中（${degraded.map((p) => pathLabel(p.path)).join("、")}上行已降级）`;
  }
  const failed = state.paths.filter((p) => p.self_check.status === "fail" || p.error != null);
  if (failed.length > 0) {
    return `采集中（${failed.map((p) => pathLabel(p.path)).join("、")}异常）`;
  }
  const pending = state.paths.some((p) => p.self_check.status === "pending");
  return pending ? "采集中（自检中）" : "采集中";
}

/**
 * sidecar 侧的采集状态一行。
 *
 * 与 [`captureSummary`] 的分工：那边是 **Rust 采集侧**的真值（设备/自检/丢帧），
 * 这里是 **对端**的真值（线上到底有没有人在推音频）。排查"UI 说在采、
 * sidecar 什么都没收到"时，这两行摆在一起就是答案。
 */
export function sidecarCaptureSummary(c: SidecarCapture): string {
  const paths = (c.active_paths ?? []).map((p) => pathLabel(p)).join("、");
  if (c.status === "connected") {
    return paths === "" ? "已连接（等待首段）" : `已连接：${paths}`;
  }
  return c.note;
}

/**
 * 诊断面板的计数行。
 *
 * 顺序固定，便于人眼对比两次刷新；**零值不隐藏**（"已丢帧 0"本身就是信息，
 * 藏起来反而看不出这一项在监控什么），但只把异常项（VAD 错误 / 上行失败 /
 * 源错误 / 重连）追加在末尾 —— 它们正常时是 0，出现即值得看。
 *
 * "已丢帧"取**源侧**口径（`source_stats`）：那才是"采集有没有跟不上"的答案；
 * `uplink.dropped` 是出站队列的丢帧（socket 跟不上），两者混在一起会让
 * 排查时分不清是哪一段堵了。
 */
export function diagnosticsRows(p: PathSnapshot): [string, string][] {
  const rows: [string, string][] = [
    ["设备", p.source_device !== "" ? p.source_device : p.device !== "" ? p.device : "（未知）"],
    ["原生格式", formatNativeFormat(p.self_check) ?? "（未知）"],
    ["VAD", p.vad_provider !== "" ? p.vad_provider : "（未启动）"],
    ["已出帧", String(p.source_stats.frames_emitted)],
    ["已丢帧", String(p.source_stats.frames_dropped)],
    ["已送段", String(p.segments_sent)],
    ["端点空洞", String(p.holes)],
    ["缓冲溢出", String(p.hold_overflow)],
    ["上行帧", String(p.uplink.frames_sent)],
    ["上行事件", String(p.uplink.events_sent)],
    ["上行丢帧", String(p.uplink.dropped)],
  ];
  if (p.uplink.reconnects > 0) rows.push(["上行重连", String(p.uplink.reconnects)]);
  if (p.vad_errors > 0) rows.push(["VAD 错误", String(p.vad_errors)]);
  if (p.last_vad_error != null) rows.push(["最近 VAD 错误", p.last_vad_error]);
  if (p.source_stats.errors > 0) rows.push(["采集源错误", String(p.source_stats.errors)]);
  if (p.push_errors > 0) rows.push(["上行失败", String(p.push_errors)]);
  if (p.uplink.send_failures > 0) {
    rows.push(["上行发送失败", String(p.uplink.send_failures)]);
  }
  if (p.uplink.reconnect_exhausted) {
    rows.push(["上行状态", `已降级（关闭码 ${p.uplink.close_code ?? "未知"}）`]);
  }
  return rows;
}
