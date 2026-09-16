/**
 * 采集接线单测：自检摘要、状态标签、诊断行。
 *
 * 这里测的都是**纯函数**，所以不需要 Tauri 运行时。之所以值得单独测，
 * 是因为这些函数决定了「用户看到采集在跑还是不跑」——判断错了，
 * 界面会理直气壮地骗人（说"采集中"而实际一路都没开）。
 */

import { describe, expect, it } from "vitest";

import {
  EMPTY_CAPTURE_STATE,
  EMPTY_SESSION_STATS,
  captureSummary,
  diagnosticsRows,
  formatNativeFormat,
  pathLabel,
  pathStatusLabel,
  rateVerdict,
  selfCheckSummary,
  sessionBadge,
  sessionRows,
  sidecarCaptureSummary,
  type CaptureState,
  type PathSnapshot,
  type SelfCheck,
  type SessionStats,
} from "./capture";

// ------------------------------------------------------------------ 夹具

function selfCheck(over: Partial<SelfCheck> = {}): SelfCheck {
  return {
    status: "pass",
    reason: null,
    device: "扬声器 (Realtek)",
    native_sample_rate: 48000,
    native_channels: 2,
    observed_frames: 17,
    observed_ms: 510,
    expected_ms: 500,
    elapsed_ms: 512,
    measured_frame_rate: 33.2,
    nominal_frame_rate: 33.33,
    ...over,
  };
}

function path(over: Partial<PathSnapshot> = {}): PathSnapshot {
  return {
    path: "loopback",
    running: true,
    device: "扬声器 (Realtek)",
    source_device: "扬声器 (Realtek)",
    source_stats: { frames_emitted: 100, frames_dropped: 0, errors: 0 },
    native_sample_rate: 48000,
    native_channels: 2,
    vad_provider: "webrtc-vad",
    vad_errors: 0,
    last_vad_error: null,
    frames_emitted: 100,
    frames_dropped: 0,
    segments_sent: 3,
    holes: 0,
    hold_overflow: 0,
    push_errors: 0,
    endpoint: {
      frames: 100,
      voiced_frames: 40,
      segments_kept: 3,
      segments_dropped: 1,
      forced_cuts: 0,
      holes: 0,
      frames_lost: 0,
    },
    uplink: {
      frames_sent: 100,
      events_sent: 6,
      dropped: 0,
      downlink_messages: 2,
      unknown_downlink: 0,
      malformed_downlink: 0,
      close_code: null,
      closed: false,
      reconnects: 0,
      reconnect_exhausted: false,
      send_failures: 0,
    },
    self_check: selfCheck(),
    error: null,
    ...over,
  };
}

function session(over: Partial<SessionStats> = {}): SessionStats {
  return { ...EMPTY_SESSION_STATS, ...over };
}

function state(over: Partial<CaptureState> = {}): CaptureState {
  return { running: true, last_error: null, paths: [path()], session: session(), ...over };
}

// ------------------------------------------------------------------ 会话标识（S2）

describe("sessionBadge", () => {
  it("会话开着时给出常驻的「录制中 + 会话 id」", () => {
    const s = sessionBadge(session({ state: "open", session_id: "ses_0001" }));
    expect(s).toContain("录制中");
    expect(s).toContain("ses_0001");
  });

  it("建立中与未录制是两种不同的说法（不糊成一句「未录制」）", () => {
    expect(sessionBadge(session({ state: "opening" }))).toBe("◌ 会话建立中");
    expect(sessionBadge(session())).toBe("○ 未录制");
  });

  it("降级要说出来：采集在跑但会话没开成，用户必须看得出来", () => {
    const s = sessionBadge(session({ begin_misses: 2 }));
    expect(s).toContain("未录制");
    expect(s).toContain("降级 2 次");
  });

  it("空状态渲染成「未录制」而不是空串（常驻标识不能是空白）", () => {
    expect(sessionBadge(EMPTY_SESSION_STATS)).not.toBe("");
  });
});

describe("sessionRows", () => {
  it("恒显示会话状态与会话 id（零值本身是信息）", () => {
    const rows = sessionRows(EMPTY_SESSION_STATS);
    expect(rows.map(([k]) => k)).toEqual(["会话状态", "会话 id"]);
    expect(rows[1][1]).toBe("（无）");
  });

  it("会话开着时状态行说「已开」", () => {
    const rows = sessionRows(session({ state: "open", session_id: "ses_0009" }));
    expect(rows[0][1]).toBe("已开");
    expect(rows[1][1]).toBe("ses_0009");
  });

  it("异常计数（降级/迟到拒收/幂等）只在非 0 时追加", () => {
    const quiet = sessionRows(session()).map(([k]) => k);
    for (const k of ["会话建立降级", "会话关闭降级", "迟到回执已拒收"]) {
      expect(quiet).not.toContain(k);
    }
    const loud = sessionRows(
      session({ begin_misses: 1, end_misses: 2, stale_results_rejected: 3, begin_duplicate: 4 }),
    );
    const keys = loud.map(([k]) => k);
    expect(keys).toContain("会话建立降级");
    expect(keys).toContain("会话关闭降级");
    expect(keys).toContain("迟到回执已拒收");
    expect(keys).toContain("重复 begin（已幂等合并）");
  });
});

// ------------------------------------------------------------------ 自检摘要

describe("selfCheckSummary", () => {
  it("通过时把设备、原生格式、实测帧率、探测窗口都写出来", () => {
    const s = selfCheckSummary(selfCheck());
    expect(s).toContain("自检通过");
    expect(s).toContain("扬声器 (Realtek)");
    expect(s).toContain("48000Hz / 2ch");
    expect(s).toContain("实测 33.2 帧/秒");
    expect(s).toContain("额定 33.3");
    expect(s).toContain("17 帧 / 510ms");
  });

  it("失败时原样引用 Rust 给的原因，不做二次解释", () => {
    const s = selfCheckSummary(
      selfCheck({ status: "fail", reason: "500ms 内只到 3 帧（少于 10 帧）：设备可能被独占或权限被拒" }),
    );
    expect(s).toBe(
      "自检失败：500ms 内只到 3 帧（少于 10 帧）：设备可能被独占或权限被拒",
    );
  });

  it("失败但没给原因时也不显示 undefined", () => {
    const s = selfCheckSummary(selfCheck({ status: "fail", reason: null }));
    expect(s).toBe("自检失败：原因未知");
  });

  it("未完成时说明在探测，而不是假装通过", () => {
    const s = selfCheckSummary(selfCheck({ status: "pending" }));
    expect(s).toContain("自检中");
    expect(s).not.toContain("通过");
  });

  it("设备与格式都未知时不编造，只留帧率与窗口", () => {
    const s = selfCheckSummary(
      selfCheck({ device: "", native_sample_rate: 0, native_channels: 0 }),
    );
    expect(s).not.toContain("Hz");
    expect(s).not.toContain("（未知）");
    expect(s).toContain("实测 33.2 帧/秒");
  });
});

// ------------------------------------------------------------------ 帧率判定

describe("rateVerdict", () => {
  it("±35% 内算正常", () => {
    expect(rateVerdict(33.33, 33.33)).toBe("ok");
    expect(rateVerdict(33.33 * 0.65, 33.33)).toBe("ok");
    expect(rateVerdict(33.33 * 1.35, 33.33)).toBe("ok");
  });

  it("明显偏低 / 偏高要能区分，而不是笼统说异常", () => {
    expect(rateVerdict(10, 33.33)).toBe("low");
    expect(rateVerdict(120, 33.33)).toBe("high");
  });

  it("0 / null / NaN 都是「未测出」，不误判为偏低", () => {
    expect(rateVerdict(0, 33.33)).toBe("unknown");
    expect(rateVerdict(null, 33.33)).toBe("unknown");
    expect(rateVerdict(Number.NaN, 33.33)).toBe("unknown");
    expect(rateVerdict(33.33, 0)).toBe("unknown");
  });
});

// ------------------------------------------------------------------ 状态标签

describe("pathStatusLabel", () => {
  it("有错误时错误优先，因为「设备打不开」比「自检失败」更根本", () => {
    const p = path({
      error: "回环设备打不开：找不到输出设备",
      self_check: selfCheck({ status: "fail", reason: "没帧" }),
    });
    expect(pathStatusLabel(p)).toBe("不可用：回环设备打不开：找不到输出设备");
  });

  it("自检失败时不说「采集中」——那正是骗人的地方", () => {
    const p = path({ self_check: selfCheck({ status: "fail", reason: "权限被拒" }) });
    expect(pathStatusLabel(p)).toBe("自检失败");
  });

  it("已停止 / 自检中 / 采集中 三态分明", () => {
    expect(pathStatusLabel(path({ running: false }))).toBe("已停止");
    expect(pathStatusLabel(path({ self_check: selfCheck({ status: "pending" }) }))).toBe("自检中");
    expect(pathStatusLabel(path())).toBe("采集中");
  });
});

describe("sessionBadge 的三种「没在录」要说清是哪种", () => {
  it("录制开关关着 ≠ 降级（诊断面板必须能区分）", () => {
    const off = sessionBadge(session({ disabled_skips: 2 }));
    expect(off).toContain("未录制");
    expect(off).toContain("未开启");
    expect(off).not.toContain("降级");

    const degraded = sessionBadge(session({ begin_misses: 1 }));
    expect(degraded).toContain("降级");
    expect(degraded).not.toContain("未开启");
  });
});

describe("captureSummary", () => {
  it("未启动时是「未采集」，不是空字符串", () => {
    expect(captureSummary(EMPTY_CAPTURE_STATE)).toBe("未采集");
  });

  it("服务级错误直接透出（例如 uplink 鉴权失败）", () => {
    expect(captureSummary(state({ running: false, last_error: "loopback: 鉴权失败" }))).toBe(
      "采集异常：loopback: 鉴权失败",
    );
  });

  it("在跑但某路异常时点名是哪一路", () => {
    const s = state({
      paths: [
        path({ path: "loopback" }),
        path({ path: "mic", error: "麦克风打不开" }),
      ],
    });
    expect(captureSummary(s)).toBe("采集中（麦克风异常）");
  });

  it("在跑且某路还在自检时如实说「自检中」", () => {
    const s = state({ paths: [path({ self_check: selfCheck({ status: "pending" }) })] });
    expect(captureSummary(s)).toBe("采集中（自检中）");
  });

  it("全部正常时只说「采集中」", () => {
    expect(captureSummary(state())).toBe("采集中");
  });
});

// ------------------------------------------------------------------ 诊断行

describe("diagnosticsRows", () => {
  it("零值也保留（「已丢帧 0」本身就是信息）", () => {
    const rows = diagnosticsRows(path());
    const asMap = Object.fromEntries(rows);
    expect(asMap["已丢帧"]).toBe("0");
    expect(asMap["端点空洞"]).toBe("0");
  });

  it("异常项只在非零时追加，正常时不占版面", () => {
    const clean = Object.fromEntries(diagnosticsRows(path()));
    expect(clean["VAD 错误"]).toBeUndefined();
    expect(clean["上行失败"]).toBeUndefined();

    const dirty = Object.fromEntries(
      diagnosticsRows(path({ vad_errors: 2, last_vad_error: "bad_frame", push_errors: 1 })),
    );
    expect(dirty["VAD 错误"]).toBe("2");
    expect(dirty["最近 VAD 错误"]).toBe("bad_frame");
    expect(dirty["上行失败"]).toBe("1");
  });

  it("未知设备显示占位符而不是空白", () => {
    const rows = Object.fromEntries(
      diagnosticsRows(
        path({
          device: "",
          source_device: "",
          vad_provider: "",
          self_check: selfCheck({ native_sample_rate: 0, native_channels: 0 }),
        }),
      ),
    );
    expect(rows["设备"]).toBe("（未知）");
    expect(rows["VAD"]).toBe("（未启动）");
    expect(rows["原生格式"]).toBe("（未知）");
  });

  it("设备名优先用源自己的说法，源没报时回落到事件通道的", () => {
    // 两者一致时没有差别；源开流失败没有 Started 事件，只有源自己知道它本该是谁。
    const fromSource = Object.fromEntries(diagnosticsRows(path({ source_device: "源说的" })));
    expect(fromSource["设备"]).toBe("源说的");
    const fallback = Object.fromEntries(
      diagnosticsRows(path({ source_device: "", device: "事件说的" })),
    );
    expect(fallback["设备"]).toBe("事件说的");
  });

  it("已丢帧取源侧口径，上行丢帧单独一行（两者混在一起没法排查）", () => {
    const rows = Object.fromEntries(
      diagnosticsRows(
        path({
          source_stats: { frames_emitted: 100, frames_dropped: 7, errors: 0 },
          uplink: { ...path().uplink, dropped: 3 },
        }),
      ),
    );
    expect(rows["已丢帧"]).toBe("7");
    expect(rows["上行丢帧"]).toBe("3");
  });

  it("重连与降级只在真发生过时才占版面", () => {
    const clean = Object.fromEntries(diagnosticsRows(path()));
    expect(clean["上行重连"]).toBeUndefined();
    expect(clean["上行状态"]).toBeUndefined();
    expect(clean["上行发送失败"]).toBeUndefined();
    expect(clean["采集源错误"]).toBeUndefined();

    const degraded = Object.fromEntries(
      diagnosticsRows(
        path({
          source_stats: { frames_emitted: 100, frames_dropped: 0, errors: 1 },
          uplink: {
            ...path().uplink,
            reconnects: 2,
            reconnect_exhausted: true,
            send_failures: 5,
            close_code: 1006,
          },
        }),
      ),
    );
    expect(degraded["上行重连"]).toBe("2");
    expect(degraded["上行发送失败"]).toBe("5");
    expect(degraded["采集源错误"]).toBe("1");
    expect(degraded["上行状态"]).toContain("已降级");
    expect(degraded["上行状态"]).toContain("1006");
  });
});

// ------------------------------------------------------------------ 上行降级与服务摘要

describe("captureSummary / sidecarCaptureSummary", () => {
  it("上行降级要压过「采集中」的乐观说法", () => {
    // 音频还在采，但已经送不出去了 —— 只说"采集中"就是骗人。
    const s = captureSummary(
      state({ paths: [path({ uplink: { ...path().uplink, reconnect_exhausted: true } })] }),
    );
    expect(s).toContain("系统回环");
    expect(s).toContain("上行已降级");
  });

  it("没在跑时就是未采集（不看 paths 是否为空）", () => {
    // 停止后 paths 仍保留上一次运行的终态，所以不能用它判断"在不在采"。
    expect(captureSummary(state({ running: false }))).toBe("未采集");
  });

  it("sidecar 侧：连着就报路名，没连就引它自己的说明", () => {
    expect(
      sidecarCaptureSummary({ status: "connected", active_paths: ["loopback", "mic"], note: "x" }),
    ).toBe("已连接：系统回环、麦克风");
    expect(sidecarCaptureSummary({ status: "connected", active_paths: [], note: "x" })).toBe(
      "已连接（等待首段）",
    );
    expect(sidecarCaptureSummary({ status: "idle", note: "尚未收到任何音频连接" })).toBe(
      "尚未收到任何音频连接",
    );
    expect(
      sidecarCaptureSummary({ status: "disconnected", note: "最近一次音频连接已断开" }),
    ).toBe("最近一次音频连接已断开");
  });
});

// ------------------------------------------------------------------ 小工具

describe("pathLabel / formatNativeFormat", () => {
  it("两路都有中文名", () => {
    expect(pathLabel("loopback")).toBe("系统回环");
    expect(pathLabel("mic")).toBe("麦克风");
  });

  it("未知路径原样透出，不吞掉信息", () => {
    expect(pathLabel("weird")).toBe("weird");
  });

  it("只有单边信息时也给出可读结果", () => {
    expect(formatNativeFormat(selfCheck({ native_channels: 0 }))).toBe("48000Hz / ?ch");
    expect(formatNativeFormat(selfCheck({ native_sample_rate: 0 }))).toBe("?Hz / 2ch");
  });
});
