//! 采集服务（M2-7 生产接线）：每路一个 VAD worker 线程 + 端点状态机 + uplink。
//!
//! # 为什么是「每路一个线程」
//!
//! [`Vad`] 刻意**不要求 `Send`**（webrtc-vad 持有 `*mut Fvad`，见 `vad.rs` 的
//! threading 节）。诚实的做法是让 VAD 在**它自己的线程上**构造与驱动，
//! 而不是给 trait 加一个替身 provider 要装的 `Send` 假象。于是：
//!
//! ```text
//! 每路：capture 线程 ──FrameQueue──▶ worker 线程 ──▶ AudioUplink（唯一出站队列）
//!                                   ├─ Vad（本线程构造）
//!                                   ├─ EndpointState
//!                                   └─ 帧与事件按同一顺序入同一个队列
//! ```
//!
//! 「事件合并进统一上行队列」是字面意义的：`segment_start` / `segment_end` /
//! `vad_state` 与音频帧走**同一个** [`AudioUplink`] 出站队列，wire seq 由
//! `uplink.push_*` 在入队时分配（R10：两类帧共享一个序号空间）。
//!
//! # 帧的上行闸门：只发段内音频
//!
//! sidecar 对「无段时的音频帧」只计 `unsolicited_audio` 后丢弃，所以帧必须
//! **跟在 `segment_start` 之后**。而端点的 onset 最早可能落在触发帧前 4 帧
//! （起始窗口 5 帧），"留不留"又要到段尾才知道（最短段 9 帧）——
//! 于是 worker 维护一个**有界缓存**：
//!
//! - 未确认的候选段：帧进缓存，不上行；
//! - `Decisions::open`：先发 `segment_start`，再把 onset 起的缓存帧按序放出；
//! - 丢弃的短段：缓存清空，线上从未出现过这一段；
//! - 已确认段：逐帧直发（含段内静音间隙与 hangover，ASR 需要自然的尾音）。
//!
//! 缓存上限 [`HOLD_CAPACITY`]（22 帧）由端点规格推导，见 `endpoint.rs` 模块注释。
//!
//! # 丢帧（R13 的代价）是显式的
//!
//! 队列满时 `DropOldestQueue` 丢最旧并计数。worker 按 `ts_ms` 发现序号跳变，
//! 交给端点当**空洞**处理：已确认段立即封口、候选段整段丢弃、窗口不回看空洞之前。
//! 把丢帧当静音会凭空造出 hangover，那是编数据，不是容错。
//!
//! # 启动自检（开流 500ms 探测）
//!
//! 开流后立刻对**真实到达的帧**做一次 500ms 探测：设备/原生格式（来自
//! `CaptureEvent::Started`）、帧是否真的在来（权限/独占）、实测帧率。
//! 探测**不额外消费帧** —— 它观察的就是主循环正在处理的那条流，
//! 所以「自检通过」与「管线在跑」是同一件事，不是两次采样。
//!
//! R14：本模块所有对外字段都是设备名/格式/计数/种类名，**不含音频与转写文本**。

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::json;

use super::endpoint::{
    Decisions, EndpointState, EndpointStats, Heartbeat, Segment, FRAME_MS, HOLD_CAPACITY,
};
use super::frame::Frame16k;
use super::loopback::{
    AudioError, CaptureEvent, CaptureStats, EventQueue, FrameQueue, LoopbackSource,
    EVENT_QUEUE_CAPACITY, FRAME_QUEUE_CAPACITY,
};
use super::uplink::{AudioUplink, EventSink, PathLabel, UplinkConfig, UplinkSnapshot};
use super::vad::{open_default as open_default_vad, VadError};

/// worker 空转节拍（同时决定停止响应延迟的上界）。
pub const WORKER_TICK: Duration = Duration::from_millis(100);
/// 自检探测时长（音频时间）。
pub const PROBE_MS: u64 = 500;
/// 自检至少看到多少帧才算"帧在来"（500ms 额定 16.7 帧，留出抖动余量）。
pub const PROBE_MIN_FRAMES: u64 = 10;
/// 自检的墙钟上限：超过就判定"流开着但没音频"。
pub const PROBE_TIMEOUT: Duration = Duration::from_millis(2500);
/// 实测帧率允许偏离额定的比例。
pub const PROBE_RATE_TOLERANCE: f64 = 0.35;
/// 额定帧率（帧/秒）。
pub const NOMINAL_FRAME_RATE: f64 = 1000.0 / FRAME_MS as f64;

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

/// 按路构造采集源。生产用 [`platform_source_factory`]；测试注入假源。
pub type SourceFactory =
    dyn Fn(PathLabel) -> Result<Box<dyn LoopbackSource>, AudioError> + Send + Sync;

/// 逐帧决策观察点（注入 E2E 用）。
///
/// 参数 = `(路, 帧序号, 是否 voiced)`，由 worker 在**真实 VAD 分类成功之后**回调，
/// 所以它记录的正是端点状态机的输入本身 —— 测试拿它去喂 Python 参考端点，
/// 复核的就是"同一串决策"而不是"重跑一遍 VAD"。带 `PathLabel` 是必需的：
/// 双路共用一个观察点时，没有路标就分不清谁的决策。
///
/// 生产路径永不设置它（`None`），代价只是一个 `Option<Arc<..>>`。
pub type DecisionTap = Arc<dyn Fn(PathLabel, u64, bool) + Send + Sync>;

/// 平台默认采集源工厂。
///
/// 平台矩阵（Phase 1b）：Windows = WASAPI 回环 + cpal 麦克风；
/// Linux = PulseAudio monitor + cpal 麦克风；macOS = **仅麦克风**
/// （无系统回环，必须显式报 `Unsupported` 而不是静默采到空音频）。
pub fn platform_source_factory() -> Arc<SourceFactory> {
    Arc::new(|path: PathLabel| match path {
        PathLabel::Mic => Ok(Box::new(super::cpal_mic::open_default_mic())),
        PathLabel::Loopback => {
            #[cfg(target_os = "windows")]
            {
                Ok(Box::new(super::wasapi_loopback::open_default_loopback()))
            }
            #[cfg(target_os = "linux")]
            {
                Ok(Box::new(super::cpal_monitor::open_default_monitor()))
            }
            #[cfg(not(any(target_os = "windows", target_os = "linux")))]
            {
                Err(AudioError::Unsupported(
                    "macOS 无系统回环（Phase 1b 仅麦克风）".to_string(),
                ))
            }
        }
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SelfCheckStatus {
    /// 还没跑完（刚开流）。
    Pending,
    Pass,
    Fail,
}

/// 开流自检报告（内容无关：设备名/格式/计数/可读原因）。
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct SelfCheck {
    pub status: SelfCheckStatus,
    /// 失败原因（可读，无用户数据）；通过时为 `None`。
    pub reason: Option<String>,
    pub device: String,
    pub native_sample_rate: u32,
    pub native_channels: u16,
    /// 探测窗口内到达的帧数。
    pub observed_frames: u64,
    /// 这些帧代表的音频时长（`frames × 30ms`）。
    pub observed_ms: u64,
    /// 探测目标时长（[`PROBE_MS`]）。
    pub expected_ms: u64,
    /// 墙钟耗时。
    pub elapsed_ms: u64,
    /// 实测帧率（帧/秒）。
    pub measured_frame_rate: f64,
    /// 额定帧率（33.33 帧/秒）。
    pub nominal_frame_rate: f64,
}

impl Default for SelfCheck {
    fn default() -> Self {
        Self::pending()
    }
}

impl SelfCheck {
    fn pending() -> Self {
        Self {
            status: SelfCheckStatus::Pending,
            reason: None,
            device: String::new(),
            native_sample_rate: 0,
            native_channels: 0,
            observed_frames: 0,
            observed_ms: 0,
            expected_ms: PROBE_MS,
            elapsed_ms: 0,
            measured_frame_rate: 0.0,
            nominal_frame_rate: NOMINAL_FRAME_RATE,
        }
    }
}

/// 单路诊断（worker 写、命令读；原子量避免锁竞争）。
#[derive(Default)]
struct PathDiag {
    device: Mutex<String>,
    native_sample_rate: AtomicU64,
    native_channels: AtomicU64,
    vad_provider: Mutex<String>,
    vad_errors: AtomicU64,
    last_vad_error: Mutex<Option<String>>,
    endpoint: Mutex<EndpointStats>,
    self_check: Mutex<SelfCheck>,
    frames_emitted: AtomicU64,
    frames_dropped: AtomicU64,
    segments_sent: AtomicU64,
    hold_overflow: AtomicU64,
    push_errors: AtomicU64,
    holes: AtomicU64,
    worker_error: Mutex<Option<String>>,
    worker_running: AtomicBool,
}

impl PathDiag {
    fn new() -> Self {
        Self {
            self_check: Mutex::new(SelfCheck::pending()),
            ..Default::default()
        }
    }

    fn set_worker_error(&self, msg: impl Into<String>) {
        let msg = msg.into();
        let mut slot = lock(&self.worker_error);
        if slot.is_none() {
            *slot = Some(msg);
        }
    }
}

/// 单路快照（进诊断面板）。
#[derive(Debug, Clone, Serialize)]
pub struct PathSnapshot {
    pub path: &'static str,
    pub running: bool,
    /// 设备名（来自 `CaptureEvent::Started`，即**事件通道**的说法）。
    pub device: String,
    /// 设备名（来自 `LoopbackSource::current_device()`，即**源自己**的说法）。
    ///
    /// 与 `device` 并存是有意的：源开流失败时没有 `Started` 事件，只有源自己
    /// 知道它本该是谁；两者不一致时也说明事件通道漏了。
    pub source_device: String,
    /// 源侧计数（`frames_emitted`/`frames_dropped`/`errors`），直接来自
    /// `LoopbackSource::stats()` —— 诊断面板上的"已丢帧"因此是源的真实口径。
    pub source_stats: CaptureStats,
    pub native_sample_rate: u32,
    pub native_channels: u16,
    pub vad_provider: String,
    /// VAD 拒绝分类的帧数（生产应恒为 0；非 0 说明帧/率契约被破坏）。
    pub vad_errors: u64,
    pub last_vad_error: Option<String>,
    pub frames_emitted: u64,
    pub frames_dropped: u64,
    /// 已上线的段数。
    pub segments_sent: u64,
    /// 因丢帧被迫封口的段数。
    pub holes: u64,
    /// 候选段缓存溢出次数（构造上应为 0；非 0 说明端点规格被改动）。
    pub hold_overflow: u64,
    /// 入队失败的帧/事件数（连接已关或本地校验不过）。
    pub push_errors: u64,
    pub endpoint: EndpointStats,
    pub uplink: UplinkSnapshot,
    pub self_check: SelfCheck,
    pub error: Option<String>,
}

/// 服务快照（Tauri 命令 / 诊断面板的唯一数据源）。
#[derive(Debug, Clone, Serialize)]
pub struct ServiceSnapshot {
    pub running: bool,
    /// 全服务级错误（如 uplink 鉴权失败）。
    pub last_error: Option<String>,
    pub paths: Vec<PathSnapshot>,
}

/// 一路的运行时句柄。
struct PathRuntime {
    path: PathLabel,
    source: Box<dyn LoopbackSource>,
    uplink: Arc<AudioUplink>,
    diag: Arc<PathDiag>,
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
    /// 源启动失败时记录（该路没有 worker）。
    start_error: Option<String>,
}

#[derive(Default)]
struct Inner {
    running: bool,
    paths: Vec<PathRuntime>,
    /// 上一次运行留下的诊断（该路快照 + 它的 uplink 句柄）。
    ///
    /// 存在的理由：`stop_sources` 必须把 `paths` 取走（要停源、join worker、
    /// 关 uplink），但**诊断面板不该在停下的那一刻变空白** —— 用户按完停止
    /// 最想看的恰恰是刚才那次跑得怎么样（丢了几帧、自检结论、上行计数）。
    ///
    /// 连 uplink 句柄一起留着而不是只留一份死快照：`closed` / `close_code`
    /// 要等 socket 真正关完才成终值，留句柄就能每次读实时值，而不是留一句
    /// 停止后必然过期的"未关闭"。
    last: Vec<(PathSnapshot, Arc<AudioUplink>)>,
    last_error: Option<String>,
}

/// 采集服务：启停双路采集 + 双 uplink，并提供诊断快照。
pub struct CaptureService {
    factory: Arc<SourceFactory>,
    /// 逐帧决策观察点（见 [`DecisionTap`]）；生产恒为 `None`。
    tap: Option<DecisionTap>,
    inner: Mutex<Inner>,
}

impl std::fmt::Debug for CaptureService {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CaptureService")
            .field("snapshot", &self.snapshot())
            .finish_non_exhaustive()
    }
}

impl Default for CaptureService {
    fn default() -> Self {
        Self::new()
    }
}

impl CaptureService {
    /// 生产服务（平台默认采集源）。
    pub fn new() -> Self {
        Self::with_source_factory(platform_source_factory())
    }

    /// 注入采集源工厂（假源全链 E2E 用）。
    pub fn with_source_factory(factory: Arc<SourceFactory>) -> Self {
        Self {
            factory,
            tap: None,
            inner: Mutex::new(Inner::default()),
        }
    }

    /// 注入逐帧决策观察点（注入 E2E 用）。
    ///
    /// 回调在 worker 线程上执行，必须**快且无阻塞**（写个 Vec 就好）。
    pub fn with_decision_tap(mut self, tap: DecisionTap) -> Self {
        self.tap = Some(tap);
        self
    }

    pub fn is_running(&self) -> bool {
        lock(&self.inner).running
    }

    /// 启动双路采集与双 uplink。
    ///
    /// 语义裁定：
    /// - 幂等：已在跑则原样返回快照（重复按快捷键不会叠出第二套线程）。
    /// - **uplink 连不上（含鉴权被拒）是致命的**：两路一起失败，返回 `Err`，
    ///   服务保持停止态 —— 采到音频却送不出去不是"部分可用"。
    /// - **单路设备打不开不是致命的**：另一路照跑，失败原因落在该路 `error`
    ///   与 `last_error` 上（macOS 无回环、Linux 无 PulseAudio 都走这条路）。
    pub async fn start(
        &self,
        port: u16,
        token: &str,
        sink: Arc<dyn EventSink>,
    ) -> Result<ServiceSnapshot, String> {
        if self.is_running() {
            return Ok(self.snapshot());
        }

        // 1) 先连两条 uplink：握手失败（401/403）要立刻暴露，而不是先开流。
        let mut uplinks: Vec<(PathLabel, Arc<AudioUplink>)> = Vec::new();
        for path in PathLabel::ALL {
            let cfg = UplinkConfig::new(port, token, path);
            match AudioUplink::connect(cfg, sink.clone()).await {
                Ok(u) => uplinks.push((path, Arc::new(u))),
                Err(e) => {
                    for (_, u) in uplinks.drain(..) {
                        u.shutdown().await;
                    }
                    let msg = format!("{}: {e}", path.as_str());
                    lock(&self.inner).last_error = Some(msg.clone());
                    return Err(msg);
                }
            }
        }

        // 2) 逐路开流 + 起 worker。
        let mut paths = Vec::new();
        let mut first_error = None;
        for (path, uplink) in uplinks {
            let frames = FrameQueue::new(FRAME_QUEUE_CAPACITY);
            let events = EventQueue::new(EVENT_QUEUE_CAPACITY);
            let mut source = match (self.factory)(path) {
                Ok(s) => s,
                Err(e) => {
                    uplink.shutdown().await;
                    let msg = format!("{}: {e}", path.as_str());
                    first_error.get_or_insert(msg.clone());
                    paths.push(PathRuntime {
                        path,
                        source: Box::new(ClosedSource::new(path.as_str(), msg.clone())),
                        uplink,
                        diag: Arc::new(PathDiag::new()),
                        stop: Arc::new(AtomicBool::new(false)),
                        worker: None,
                        start_error: Some(msg),
                    });
                    continue;
                }
            };
            if let Err(e) = source.start(frames.clone(), events.clone()) {
                uplink.shutdown().await;
                let msg = format!("{}: {e}", path.as_str());
                first_error.get_or_insert(msg.clone());
                paths.push(PathRuntime {
                    path,
                    source,
                    uplink,
                    diag: Arc::new(PathDiag::new()),
                    stop: Arc::new(AtomicBool::new(false)),
                    worker: None,
                    start_error: Some(msg),
                });
                continue;
            }

            let diag = Arc::new(PathDiag::new());
            let stop = Arc::new(AtomicBool::new(false));
            let spawned = {
                let diag = diag.clone();
                let stop = stop.clone();
                let uplink = uplink.clone();
                let tap = self.tap.clone();
                thread::Builder::new()
                    .name(format!("vad-{}", path.as_str()))
                    .spawn(move || worker_loop(path, frames, events, uplink, diag, stop, tap))
            };
            let worker = match spawned {
                Ok(h) => Some(h),
                Err(e) => {
                    // 线程都起不来（极端资源枯竭）：把这一路停掉并如实记录，
                    // 不留一个"源在跑但没人消费"的僵尸路。
                    source.stop();
                    let msg = format!("{}: spawn vad worker: {e}", path.as_str());
                    first_error.get_or_insert(msg.clone());
                    paths.push(PathRuntime {
                        path,
                        source,
                        uplink,
                        diag,
                        stop,
                        worker: None,
                        start_error: Some(msg),
                    });
                    continue;
                }
            };
            paths.push(PathRuntime {
                path,
                source,
                uplink,
                diag,
                stop,
                worker,
                start_error: None,
            });
        }

        let mut inner = lock(&self.inner);
        inner.paths = paths;
        inner.running = true;
        inner.last_error = first_error;
        drop(inner);
        Ok(self.snapshot())
    }

    /// 同步停：置位 → 停源 → join worker；返回待关闭的 uplink。
    ///
    /// 供异步 [`CaptureService::stop`] 与退出路径复用。
    ///
    /// 停止后把**终态**逐路快照存进 `inner.last`：计数在 join 之后读才是终值
    /// （源线程与 worker 都已退出），而且诊断面板要能继续显示它。
    pub fn stop_sources(&self) -> Vec<Arc<AudioUplink>> {
        let paths = {
            let mut inner = lock(&self.inner);
            inner.running = false;
            std::mem::take(&mut inner.paths)
        };
        let mut uplinks = Vec::new();
        let mut last = Vec::with_capacity(paths.len());
        for mut p in paths {
            p.stop.store(true, Ordering::SeqCst);
            p.source.stop();
            if let Some(h) = p.worker.take() {
                let _ = h.join();
            }
            last.push((path_snapshot(&p), p.uplink.clone()));
            uplinks.push(p.uplink);
        }
        lock(&self.inner).last = last;
        uplinks
    }

    /// 异步停：停源 + 等 socket 优雅关闭。
    ///
    /// join 上界是 `PUMP_TICK + WORKER_TICK`（各 100ms），故直接在异步上下文里
    /// 同步执行即可，不需要 `spawn_blocking`。
    pub async fn stop(&self) -> ServiceSnapshot {
        for u in self.stop_sources() {
            u.shutdown().await;
        }
        self.snapshot()
    }

    /// 退出路径：只停源与 worker，不等 socket 关闭（进程马上结束）。
    pub fn request_stop(&self) {
        let _ = self.stop_sources();
    }

    /// 采集开关。返回切换后的快照。
    pub async fn toggle(
        &self,
        port: u16,
        token: &str,
        sink: Arc<dyn EventSink>,
    ) -> Result<ServiceSnapshot, String> {
        if self.is_running() {
            Ok(self.stop().await)
        } else {
            self.start(port, token, sink).await
        }
    }

    /// 服务快照（Tauri 命令 / 诊断面板的唯一数据源）。
    ///
    /// `paths` 的语义是「**当前**在跑的每路；没在跑时就是上一次运行的终态」——
    /// 于是"按下停止"不会把诊断面板清空，而 `running` 仍然如实反映状态。
    pub fn snapshot(&self) -> ServiceSnapshot {
        let inner = lock(&self.inner);
        let paths = if inner.running {
            inner.paths.iter().map(path_snapshot).collect()
        } else {
            inner
                .last
                .iter()
                .map(|(p, u)| {
                    let mut p = p.clone();
                    // 实时读，不留过期值（见 `Inner::last` 的注释）。
                    p.uplink = u.stats();
                    p
                })
                .collect()
        };
        ServiceSnapshot {
            running: inner.running,
            last_error: inner.last_error.clone(),
            paths,
        }
    }
}

fn path_snapshot(p: &PathRuntime) -> PathSnapshot {
    let d = &p.diag;
    PathSnapshot {
        path: p.path.as_str(),
        running: d.worker_running.load(Ordering::Relaxed),
        device: lock(&d.device).clone(),
        // 源自己的说法（可能与事件通道的 `device` 不同，也可能在源没报
        // `Started` 时是唯一非空的那个）。
        source_device: p.source.current_device(),
        source_stats: p.source.stats(),
        native_sample_rate: d.native_sample_rate.load(Ordering::Relaxed) as u32,
        native_channels: d.native_channels.load(Ordering::Relaxed) as u16,
        vad_provider: lock(&d.vad_provider).clone(),
        vad_errors: d.vad_errors.load(Ordering::Relaxed),
        last_vad_error: lock(&d.last_vad_error).clone(),
        frames_emitted: d.frames_emitted.load(Ordering::Relaxed),
        frames_dropped: d.frames_dropped.load(Ordering::Relaxed),
        segments_sent: d.segments_sent.load(Ordering::Relaxed),
        holes: d.holes.load(Ordering::Relaxed),
        hold_overflow: d.hold_overflow.load(Ordering::Relaxed),
        push_errors: d.push_errors.load(Ordering::Relaxed),
        endpoint: *lock(&d.endpoint),
        uplink: p.uplink.stats(),
        self_check: lock(&d.self_check).clone(),
        error: p
            .start_error
            .clone()
            .or_else(|| lock(&d.worker_error).clone()),
    }
}

/// 占位源：源构造/启动失败时用它填位，使该路在快照里可见且可解释。
struct ClosedSource {
    label: &'static str,
    reason: String,
}

impl ClosedSource {
    fn new(label: &'static str, reason: String) -> Self {
        Self { label, reason }
    }
}

impl LoopbackSource for ClosedSource {
    fn start(
        &mut self,
        _frames: Arc<FrameQueue>,
        events: Arc<EventQueue>,
    ) -> Result<(), AudioError> {
        events.push(CaptureEvent::Error {
            message: self.reason.clone(),
        });
        Err(AudioError::Stream(self.reason.clone()))
    }

    fn stop(&mut self) {}

    fn current_device(&self) -> String {
        self.label.to_string()
    }

    fn stats(&self) -> super::loopback::CaptureStats {
        Default::default()
    }
}

/// 自检探测：只观察主循环正在处理的帧，不额外消费。
struct Probe {
    started: Instant,
    frames: u64,
    finished: bool,
}

impl Probe {
    fn new() -> Self {
        Self {
            started: Instant::now(),
            frames: 0,
            finished: false,
        }
    }

    fn observe(&mut self) {
        if !self.finished {
            self.frames += 1;
        }
    }

    /// 探测窗口满了（音频时间够 500ms）或墙钟超时。
    fn due(&self) -> bool {
        !self.finished
            && (self.frames * FRAME_MS >= PROBE_MS || self.started.elapsed() >= PROBE_TIMEOUT)
    }

    /// 结算一次（幂等：只算第一次）。
    fn finish(&mut self, diag: &PathDiag) {
        if self.finished {
            return;
        }
        self.finished = true;
        let elapsed = self.started.elapsed();
        let observed_ms = self.frames * FRAME_MS;
        let elapsed_s = elapsed.as_secs_f64().max(1e-3);
        let measured = self.frames as f64 / elapsed_s;
        let (status, reason) = if self.frames < PROBE_MIN_FRAMES {
            (
                SelfCheckStatus::Fail,
                Some(format!(
                    "开流 {}ms 只收到 {} 帧音频（额定约 {} 帧）：设备可能被独占、\
                     麦克风权限被拒，或回环源没有音频在播",
                    elapsed.as_millis(),
                    self.frames,
                    PROBE_MS / FRAME_MS
                )),
            )
        } else if measured < NOMINAL_FRAME_RATE * (1.0 - PROBE_RATE_TOLERANCE) {
            (
                SelfCheckStatus::Fail,
                Some(format!(
                    "实测帧率 {measured:.1}/s 明显低于额定 {NOMINAL_FRAME_RATE:.1}/s：\
                     设备/驱动供数跟不上"
                )),
            )
        } else if measured > NOMINAL_FRAME_RATE * (1.0 + PROBE_RATE_TOLERANCE) {
            (
                SelfCheckStatus::Fail,
                Some(format!(
                    "实测帧率 {measured:.1}/s 明显高于额定 {NOMINAL_FRAME_RATE:.1}/s：\
                     采样率或重采样链路异常"
                )),
            )
        } else {
            (SelfCheckStatus::Pass, None)
        };
        *lock(&diag.self_check) = SelfCheck {
            status,
            reason,
            device: lock(&diag.device).clone(),
            native_sample_rate: diag.native_sample_rate.load(Ordering::Relaxed) as u32,
            native_channels: diag.native_channels.load(Ordering::Relaxed) as u16,
            observed_frames: self.frames,
            observed_ms,
            expected_ms: PROBE_MS,
            elapsed_ms: elapsed.as_millis() as u64,
            measured_frame_rate: measured,
            nominal_frame_rate: NOMINAL_FRAME_RATE,
        };
    }
}

/// worker 主循环（每路一个线程；VAD 在本线程构造）。
fn worker_loop(
    path: PathLabel,
    frames: Arc<FrameQueue>,
    events: Arc<EventQueue>,
    uplink: Arc<AudioUplink>,
    diag: Arc<PathDiag>,
    stop: Arc<AtomicBool>,
    tap: Option<DecisionTap>,
) {
    let mut vad = match open_default_vad() {
        Ok(v) => v,
        Err(e) => {
            // 无法构造 VAD：不静默降级成"全静音"，而是把这一路标成错误。
            diag.set_worker_error(format!("vad 不可用: {e}"));
            return;
        }
    };
    *lock(&diag.vad_provider) = vad.provider().to_string();
    diag.worker_running.store(true, Ordering::SeqCst);

    let mut ep = EndpointState::new();
    let mut hb = Heartbeat::new();
    let mut held: VecDeque<Frame16k> = VecDeque::with_capacity(HOLD_CAPACITY);
    let mut committed = false;
    let mut probe = Probe::new();

    while !stop.load(Ordering::SeqCst) {
        drain_capture_events(&events, &diag);

        if let Some(frame) = frames.pop_timeout(WORKER_TICK) {
            diag.frames_emitted.fetch_add(1, Ordering::Relaxed);
            diag.frames_dropped
                .store(frames.dropped(), Ordering::Relaxed);
            probe.observe();
            process_frame(
                path,
                frame,
                vad.as_mut(),
                &mut ep,
                &mut hb,
                &mut held,
                &mut committed,
                &uplink,
                &diag,
                tap.as_ref(),
            );
        }

        if probe.due() {
            probe.finish(&diag);
        }
        *lock(&diag.endpoint) = ep.stats();
    }

    // 收尾：流结束时还开着的段要封口（与参考实现的 `if in_seg: _close` 同义）。
    let d = ep.finish();
    if let Some(c) = d.close {
        if c.committed {
            send_end(path, c.segment, &uplink, &diag);
        }
    }
    if committed {
        for f in held.drain(..) {
            push_frame(&uplink, &f, &diag);
        }
    }
    drain_capture_events(&events, &diag);
    *lock(&diag.endpoint) = ep.stats();
    diag.worker_running.store(false, Ordering::SeqCst);
}

fn drain_capture_events(events: &EventQueue, diag: &PathDiag) {
    while let Some(ev) = events.try_pop() {
        match ev {
            CaptureEvent::Started {
                device,
                sample_rate,
                channels,
            } => {
                *lock(&diag.device) = device;
                diag.native_sample_rate
                    .store(sample_rate as u64, Ordering::Relaxed);
                diag.native_channels
                    .store(channels as u64, Ordering::Relaxed);
            }
            CaptureEvent::DeviceChanged { old, new } => {
                // 插拔重建：设备名更新，格式等下一帧 Started 再刷。
                *lock(&diag.device) = format!("{new}（原 {old}）");
            }
            CaptureEvent::StreamRebuilt { device } => {
                *lock(&diag.device) = device;
            }
            CaptureEvent::Warning { message } => {
                diag.set_worker_error(message);
            }
            CaptureEvent::Error { message } => {
                diag.set_worker_error(message);
            }
            CaptureEvent::Stopped => {}
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn process_frame(
    path: PathLabel,
    frame: Frame16k,
    vad: &mut dyn super::vad::Vad,
    ep: &mut EndpointState,
    hb: &mut Heartbeat,
    held: &mut VecDeque<Frame16k>,
    committed: &mut bool,
    uplink: &AudioUplink,
    diag: &PathDiag,
    tap: Option<&DecisionTap>,
) {
    let index = frame.ts_ms / FRAME_MS;
    let voiced = match vad.is_voiced(&frame.pcm_i16) {
        Ok(v) => v,
        Err(e) => {
            // vad.rs 的核心纪律：分类失败绝不伪装成静音。这里**跳过该帧**并计数，
            // 下一帧的序号跳变会被端点当空洞处理并显式封口/丢弃 —— 症状可见，
            // 而不是悄悄变成"全静音"。
            diag.vad_errors.fetch_add(1, Ordering::Relaxed);
            let kind = match e {
                VadError::BadAggressiveness(_) => "bad_aggressiveness",
                VadError::BadFrameLength { .. } => "bad_frame_length",
                VadError::Unavailable(_) => "unavailable",
            };
            *lock(&diag.last_vad_error) = Some(kind.to_string());
            return;
        }
    };

    // 观察点必须在**分类成功之后**、端点之前：它记录的是端点的输入本身。
    // 分类失败的那一帧不会到这里（上面已 return），所以观察序列与端点看到的
    // 序列逐帧对齐 —— 这正是注入 E2E 能拿它做参考复核的前提。
    if let Some(t) = tap {
        t(path, index, voiced);
    }

    let d = ep.push(index, voiced);
    apply_decisions(path, &d, &frame, committed, held, uplink, diag);

    // 心跳：按**音频时间**每 1s 一次（墙钟会随掉帧漂移）。
    if hb.due(frame.ts_ms)
        && uplink
            .push_event(&json!({"event": "vad_state", "path": path.as_str(),
                                "ts_ms": frame.ts_ms}))
            .is_err()
    {
        diag.push_errors.fetch_add(1, Ordering::Relaxed);
    }
}

/// 把一帧的决策落到线上：顺序恒为 `segment_start` → 帧 → `segment_end`。
fn apply_decisions(
    path: PathLabel,
    d: &Decisions,
    frame: &Frame16k,
    committed: &mut bool,
    held: &mut VecDeque<Frame16k>,
    uplink: &AudioUplink,
    diag: &PathDiag,
) {
    if let Some(c) = d.hole {
        diag.holes.fetch_add(1, Ordering::Relaxed);
        if c.committed {
            send_end(path, c.segment, uplink, diag);
        }
        held.clear();
        *committed = false;
    }

    if let Some(onset) = d.open {
        // onset 之前的缓存帧不属于本段（是段前静音），丢弃。
        while held
            .front()
            .map(|f| f.ts_ms / FRAME_MS < onset)
            .unwrap_or(false)
        {
            held.pop_front();
        }
        send_start(path, onset, uplink, diag);
        for f in held.drain(..) {
            push_frame(uplink, &f, diag);
        }
        *committed = true;
    }

    if *committed {
        push_frame(uplink, frame, diag);
    }

    if let Some(c) = d.close {
        if c.committed {
            send_end(path, c.segment, uplink, diag);
        }
        held.clear();
        *committed = false;
    }

    if !*committed {
        if held.len() >= HOLD_CAPACITY {
            // 构造上不可达（见 endpoint.rs 的 HOLD_CAPACITY 推导）；真出现说明
            // 端点规格被改动，计数而不是静默丢帧。
            held.pop_front();
            diag.hold_overflow.fetch_add(1, Ordering::Relaxed);
        }
        held.push_back(frame.clone());
    }
}

fn push_frame(uplink: &AudioUplink, frame: &Frame16k, diag: &PathDiag) {
    if uplink.push_frame(frame).is_err() {
        diag.push_errors.fetch_add(1, Ordering::Relaxed);
    }
}

fn send_start(path: PathLabel, onset: u64, uplink: &AudioUplink, diag: &PathDiag) {
    let seg = Segment {
        onset,
        offset: onset,
    };
    if uplink
        .push_event(&json!({"event": "segment_start", "path": path.as_str(),
                            "ts_ms": seg.start_ms()}))
        .is_err()
    {
        diag.push_errors.fetch_add(1, Ordering::Relaxed);
    }
}

fn send_end(path: PathLabel, seg: Segment, uplink: &AudioUplink, diag: &PathDiag) {
    if uplink
        .push_event(&json!({"event": "segment_end", "path": path.as_str(),
                            "ts_ms": seg.end_ms()}))
        .is_err()
    {
        diag.push_errors.fetch_add(1, Ordering::Relaxed);
        return;
    }
    diag.segments_sent.fetch_add(1, Ordering::Relaxed);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ready_diag() -> Arc<PathDiag> {
        let diag = Arc::new(PathDiag::new());
        *lock(&diag.device) = "Fake Device".to_string();
        diag.native_sample_rate.store(48_000, Ordering::Relaxed);
        diag.native_channels.store(2, Ordering::Relaxed);
        diag
    }

    #[test]
    fn probe_passes_when_frames_arrive_at_the_nominal_rate() {
        let diag = ready_diag();
        let mut probe = Probe::new();
        // 17 帧 = 510ms ≥ 500ms → 探测到期；墙钟同步推进以得到合理帧率。
        for _ in 0..17 {
            probe.observe();
            std::thread::sleep(Duration::from_millis(28));
        }
        assert!(probe.due(), "17 frames must close the 500ms window");
        probe.finish(&diag);
        let sc = lock(&diag.self_check).clone();
        assert_eq!(sc.status, SelfCheckStatus::Pass, "{sc:?}");
        assert_eq!(sc.reason, None);
        assert_eq!(sc.device, "Fake Device");
        assert_eq!(sc.native_sample_rate, 48_000);
        assert_eq!(sc.native_channels, 2);
        assert_eq!(sc.observed_frames, 17);
        assert_eq!(sc.observed_ms, 510);
        assert_eq!(sc.expected_ms, PROBE_MS);
        assert!(
            sc.measured_frame_rate > 20.0 && sc.measured_frame_rate < 50.0,
            "measured {}",
            sc.measured_frame_rate
        );
    }

    #[test]
    fn probe_fails_with_a_readable_reason_when_no_audio_arrives() {
        let diag = ready_diag();
        let mut probe = Probe::new();
        probe.observe(); // 只有 1 帧，远低于 PROBE_MIN_FRAMES
        probe.started = Instant::now() - PROBE_TIMEOUT;
        assert!(probe.due(), "the wall-clock timeout must close the probe");
        probe.finish(&diag);
        let sc = lock(&diag.self_check).clone();
        assert_eq!(sc.status, SelfCheckStatus::Fail);
        let reason = sc.reason.expect("a failure must explain itself");
        assert!(reason.contains("只收到 1 帧"), "{reason}");
        assert!(reason.contains("权限"), "{reason}");
    }

    #[test]
    fn probe_flags_an_implausibly_low_frame_rate() {
        let diag = ready_diag();
        let mut probe = Probe::new();
        for _ in 0..12 {
            probe.observe();
        }
        // 12 帧 / 1.4s ≈ 8.6/s，远低于额定 33.3/s。
        probe.started = Instant::now() - Duration::from_millis(1400);
        probe.finish(&diag);
        let sc = lock(&diag.self_check).clone();
        assert_eq!(sc.status, SelfCheckStatus::Fail);
        assert!(sc.reason.as_deref().unwrap().contains("低于额定"), "{sc:?}");
    }

    #[test]
    fn probe_flags_an_implausibly_high_frame_rate() {
        let diag = ready_diag();
        let mut probe = Probe::new();
        for _ in 0..17 {
            probe.observe();
        }
        // 17 帧塞进 100ms → 170/s，明显高于额定。
        probe.started = Instant::now() - Duration::from_millis(100);
        probe.finish(&diag);
        let sc = lock(&diag.self_check).clone();
        assert_eq!(sc.status, SelfCheckStatus::Fail);
        assert!(sc.reason.as_deref().unwrap().contains("高于额定"), "{sc:?}");
    }

    #[test]
    fn probe_runs_once_and_then_stays_put() {
        let diag = ready_diag();
        let mut probe = Probe::new();
        for _ in 0..17 {
            probe.observe();
        }
        probe.finish(&diag);
        let first = lock(&diag.self_check).clone();
        // 之后的帧不再计入，结算也不再改写结果。
        for _ in 0..100 {
            probe.observe();
        }
        probe.finish(&diag);
        let second = lock(&diag.self_check).clone();
        assert_eq!(first, second, "the probe must not be recomputed");
        assert_eq!(second.observed_frames, 17);
    }

    #[test]
    fn platform_factory_reports_macos_loopback_as_unsupported() {
        // 平台矩阵必须显式：macOS 在 Phase 1b 只有麦克风。
        let f = platform_source_factory();
        match (f)(PathLabel::Loopback) {
            Ok(_) => {}
            Err(e) => {
                #[cfg(not(any(target_os = "windows", target_os = "linux")))]
                assert!(matches!(e, AudioError::Unsupported(_)), "{e:?}");
                #[cfg(any(target_os = "windows", target_os = "linux"))]
                panic!("loopback source must be constructible here: {e:?}");
            }
        }
        // 麦克风三平台都能构造。
        assert!((f)(PathLabel::Mic).is_ok());
    }

    #[test]
    fn closed_source_is_visible_in_the_snapshot() {
        let src = ClosedSource::new("loopback", "no device".to_string());
        assert_eq!(src.current_device(), "loopback");
        assert_eq!(src.stats().frames_emitted, 0);
    }

    #[test]
    fn service_starts_idle_and_stop_is_safe_without_a_sidecar() {
        let svc = CaptureService::new();
        assert!(!svc.is_running());
        let snap = svc.snapshot();
        assert!(!snap.running);
        assert!(snap.paths.is_empty());
        assert!(svc.stop_sources().is_empty());
        svc.request_stop(); // 空转安全
        assert!(!svc.is_running());
    }

    #[test]
    fn segment_wire_timestamps_match_the_sidecar_duration_contract() {
        let seg = Segment {
            onset: 10,
            offset: 19,
        };
        assert_eq!(seg.start_ms(), 300);
        assert_eq!(seg.end_ms(), 600);
        assert_eq!(seg.end_ms() - seg.start_ms(), seg.span_ms());
    }
}
