# InterviewCopilot（QA）— 项目长期约定

## 跑测试的环境配方（每次都要，漏了就假失败）

```bash
export PATH="/c/Users/Administrator/.cargo/bin:$PATH"          # Rust 不在 PATH 上
export NO_PROXY="127.0.0.1,localhost"                          # 本机有系统级 http_proxy(127.0.0.1:7897)，会把本机端口拦成 502
export INTERVIEWCOPILOT_PYTHON="C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"
export CARGO_INCREMENTAL=0                                      # 见下：增量缓存会诱发 rustc ICE
```

- **`CARGO_INCREMENTAL=0` 是必需的**（2026-09-15 实测）：`target/debug/incremental` 损坏后
  rustc 1.98.1 会稳定报 `the compiler unexpectedly panicked. This is a bug`（ICE，exit 101），
  且**重跑必现**、与代码无关（同一棵树在损坏前刚编过并通过 124 条）。
  关掉增量即恢复。**不要去 `rm -rf target/debug/incremental`**——本机有批量删除守卫
  （~50 文件/回合），改环境变量更快更安全。

- **`cargo test` 里凡是会拉起真实 sidecar 的集成测试**（`audio_ws_e2e`、`inject_e2e`）
  **必须**有后两个变量，否则报错/超时，**与代码无关**。缺了它们时 `audio_ws_e2e` 3 条全红。
- 隔离 venv：`~/.workbuddy-ai/binaries/python/envs/default`（不是 versions/3.13.12）。
  该 venv 需按 `sidecar/requirements-lock.txt` 装齐：`websockets`（缺了 uvicorn `ws="auto"`
  对 `/audio/stream` 直接 **404**，不是 403）、`openpyxl`、`pypdf`、`pytest`、`transformers`。
- **`cargo test` 的失败要区分「代码错」与「环境缺」**：先看是不是 ModuleNotFoundError /
  连不上 sidecar，再怀疑代码。
- **偶发 `SIGTERM`（exit 1，无输出）**：`cargo test` 全量、`git push`、`tasklist` 都遇到过，
  同一条命令**直接重跑即成功**。不要据此判定失败，务必用独立命令复核实际状态
  （push 后用 `git ls-remote --heads origin` 对 sha，而不是相信 push 的退出码）。
- 全量 `cargo test`（会同时起多个 python sidecar 子进程）容易撞资源；拆成
  `--lib` + 逐个 `--test <name>` 更稳，结果等价。

## 判定「是不是我改坏的」

- 本机 `.git/refs/remotes/**` 写入被静默丢弃 → `git status -sb` 的 `[gone]` 是假的，
  核实远端用 `git ls-remote --heads origin`。
- `target/` 里偶发 `os error 5 拒绝访问` 是环境噪声，不是 lint/编译错误；重跑即可。
- 归因方法：`git status --porcelain -- <dir>` 看改动文件集，确认失败点是否落在改动集内。

## 音频管线（M2）事实

- `Vad` trait 故意 `!Send` → **每路一个 worker 线程自建 VAD 实例**，不能共享。
- `CapturePipeline` **没有 `flush()`**：停止时还留在 `backlog` / `ready` / rubato
  内部延迟里的样本不会变成帧。**实测上界**（`resample.rs` 的
  `stop_time_in_flight_tail_is_bounded_by_the_buffers_themselves` 钉住）：
  16k 直通 / 44.1k / 48k 最坏 **< 1 帧（≈30 ms）**，22.05k 最坏 **≈2 帧（59.4 ms）**，
  硬上界 ≤ 3 帧（90 ms）。**是几十毫秒，不是几百毫秒**（此前记为「几百 ms」是错的，
  2026-09-15 已实测更正）。且大部分不可挽回——不满 480 的零头发不出去，
  flush 最多补回一帧 → 判定不值得修。
- 端点常量：`FRAME_MS=30` / `START_WINDOW=5` / `START_MIN_VOICED=3` /
  `END_SILENCE_FRAMES=17` / `MIN_KEEP_FRAMES=9` / `MAX_SEG_FRAMES=500` / `HOLD_CAPACITY=21`
  （`endpoint.rs` 注释为准，`service.rs` docstring 的 22 是旧值）。
- 段边界与 sidecar 的换算：`start_ms = onset*30`，`end_ms = (offset+1)*30`；
  sidecar 侧 `duration_ms = ts_end - ts_start`。
- 黄金向量唯一来源：`sidecar/src/audio_eval/endpoint.py::detect_segments`。
  **不要用 Rust 重写一份参考实现**去比对（那是"拿我的实现当标准"）——
  经 `scripts/endpoint_reference.py` 子进程调用同一份 Python 实现。

## 窗口 / 托盘 / 自启（taskP7）事实

- `tauri = { features = ["tray-icon", "image-png", "image-ico"] }` ——
  **`tray-icon` 不是默认 feature**；`image-*` 只有用 `tauri::include_image!` 才需要
  （`default_window_icon()` 不需要）。
- Windows 捕获排除：`windows-sys 0.61` 的
  `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE /* 17u32 */)`。
  **`window.hwnd()` 返回 `windows::HWND(pub *mut c_void)`，而 windows-sys 的 `HWND`
  就是 `*mut c_void`** → 裸指针直传，**不要为了这个引入 `windows` crate**。
- macOS 等价物走 `WebviewWindowBuilder::content_protected(true)`
  （tao 内部 `ns_window.setSharingType(NSWindowSharingType::None)`），无 cfg 门控。
- `tauri_plugin_autostart`：插件 `setup` **不写注册表**（只解析 `current_exe()`），
  所以启动期不存在「自启注册失败」。但 `ManagerExt::autolaunch()` 内部是 `state()`，
  **取不到会 panic** → 要失败不致命就用 `app.try_state::<AutoLaunchManager>()`。
- `tauri::tray::MouseButtonState` 的 doc 注释把 `Up`/`Down` **写反了**；
  实测 `Up` = 松开。左键单击只能认一个方向。
- `WebviewUrl::App` 收 `PathBuf`，给第二个窗口传参数**别塞 query**；
  用 `initialization_script` 注入 `window.__XXX__` 全局变量（也不用改 Vite 配置）。
- **新窗口必须单独进 `capabilities/default.json` 的 `windows` 数组**（现为
  `["main", "teleprompter"]`），否则新窗口没有权限。
- 提词窗（label `teleprompter`）是主窗「实时提词」页的**镜像**：
  主窗 `useLiveQA({broadcast:true})` → `emit("teleprompter://cards")`，
  提词窗只订阅渲染。**不给提词窗开第二份会话**（否则同一问题发两次检索）。
- 主窗「关闭」= **收进托盘**（`tray::close_plan`），退出走托盘「退出」；
  `tray_ready=false` 时放行真退出（否则托盘没建起来就关不掉程序）。
- 隐身边界：`docs/stealth-boundary.md`（做什么 / **永远不做什么** / 不承诺什么）。
  改这块代码前先读它，尤其 §2 的 10 项禁止清单。

## ⚠️ 并发写者纪律（2026-09-15 事故后立的硬规则）

本机**可能同时有多个会话在写同一个工作树**（实例：taskP7 与「P5 双 embedding」交叉在
`docs/PROGRESS.md` / `docs/api-contract.md` / `src/pages/Settings.tsx`）。

1. **不要用 `git show HEAD:<file> > <file>` 复位文件来做 hunk 切分** —— 会静默擦掉
   并发会话未提交的改动。本次真擦掉了，靠动手前的 `cp` 备份才恢复。
2. 要按功能切提交，先 `ls -la --time-style=+%H:%M:%S <files>` 对 `date +%H:%M:%S`
   看 mtime；有刚被写过的文件就先别动。
3. 并发下切提交的正确姿势：独立 worktree（`git worktree add --detach <tmp> HEAD`），
   主工作树一个字节不碰；但挪 ref 前仍要确认没有别的写者（对方可能刚提交）。
4. 动任何交叉文件前，先 `cp` 到 `.workbuddy-ai/backup/`（该目录已 gitignore）。
5. 报告要**如实**：擦掉了什么、怎么恢复的、恢复证据（`diff -q` 逐字节一致）都要写。

## 评测集与合成语料（2026-09-15 扩库后的事实）

- 语料 = `sidecar/tests/eval/seed_demo.json`，**108 QA + 29 字段 / 6 类目**
  （公司信息 8〔首版冻结〕+ 物流配送/退换货/支付与发票/会员与优惠/产品与规格 各 20）。
  文件名 `seed_demo` 是历史遗留，**它就是唯一语料，没有第二份**。
- 题目 = `sidecar/tests/eval/questions_100.jsonl`，**100 行 / 87 scored / 13 null**
  （null 占比硬约束 **10~15/100**，`test_eval.py` 里是区间断言）。
  追加纪律：**只追加，不改已有行**（README：冻结后不得为提分而改）。
- 出题引用的 `expected_qa_id` 必须真在语料里，否则该题永不命中且 runner 不报错
  → 有 `test_every_expected_id_resolves_to_the_corpus` 兜着。
- **`test_eval.py` 硬编码了题目数 / null 数 / real 标签数** —— 任何扩库扩题都要同步改。
- 跑基线：`python scripts/eval_baseline.py --section v3 --before docs/eval-v2-summary.json`
  （`--section` 只替换同名节，历史节原样保留；`--before` 用于生成对照表）。
  摘要落 `docs/eval-v<节名>-summary.json`。

## 检索引擎的三个已知结构性缺口（v3 基线暴露，**未修**，属 D6）

都在 `docs/eval-baseline.md` §v3「已知缺口」里有逐条证据。**动引擎前先读那一节。**

1. **字段路 entity 级联动**：字段命中把**同 entity 全部 QA** 拉到 `s.field=1.0`
   （不是命中字段自己的 0.8）→ entity 内 20 篇被拉平，排序退化为 bm25 噪声
   （`发票怎么开` top3 极差 0.003 却越过 TH_DIRECT → **direct 错答**）。
2. **`instr(alias, kw)` 子串判定**：`field_vocab.json` 里 `发货周期` 的别名含 `多久发货`
   → 关键词 `多久` 命中该字段 → 跨 entity 污染（`保修期多久` 被拉成 eval-001 direct）。
   注意 `field_lookup` 注释里「词表里没有 `支持`」的论证**没覆盖这种情况**。
3. **对抗性 null 会过期**：`有纸质发票吗` 在 8 篇语料下拒答，108 篇发票题群下变 direct。
   null 的有效性依赖「语料里没有同词族答案」，语料一变就要重标。

## 两条通用教训（本项目反复出现）

- **小语料的漂亮指标不是质量，是仪器。** v1「零答错」是低召回副产品；
  v2「Top-3 0.980」是 8 文档语料的产物（同题换 108 篇 → 0.796、答错 1→11）。
  **报召回指标必须同时报 qa_docs。**
- **改测试的隐式假设会被扩库打穿**：`test_answer_router` 依赖 `退货期限 → direct`，
  扩库后靠 jieba/simple 的额外票侥幸保住。出题/扩库前先把 seed 消费者的断言清单拉出来。
