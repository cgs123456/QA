# 实测档案：sidecar onedir 打包 + ASR 时延（task-4 建，task-10 / task-15 扩）

> 本文收录两类实测：**打包体积与启动**（第 1–5 节，task-4/10）与
> **ASR 时延**（末节，task-15）。两者口径不同，勿混用。

> 打包范围：sidecar-only 体积与启动。整包 Standard / Full 两档待 Tauri 壳联调后定（D4）。

## 方法

```sh
python sidecar/build.py                       # PyInstaller onedir
python scripts/verify-packaged.py             # §3.13 四条校验（cwd=C:\Windows\System32）
```

## 体积（onedir 总和）

| 项 | 值 |
|---|---|
| `dist-sidecar/interviewcopilot-sidecar` 总计 | **200,060,425 bytes（190.8 MiB，两次运行一致）** |
| TOP1 `interviewcopilot-sidecar.exe` | 30.92 MiB（含 transformers/numpy 纯代码） |
| TOP2 `_internal/numpy.libs/libscipy_openblas64…dll` | 19.64 MiB |
| TOP3 `_internal/onnxruntime/capi/*_pybind11_state.pyd` | 18.23 MiB |
| TOP4 `_internal/onnxruntime/capi/onnxruntime.dll` | 17.59 MiB |
| TOP5 `_internal/hf_xet/hf_xet.pyd` | 9.06 MiB |

机器行（体积回归用）：`BUNDLE_BYTES=200060425`（Windows onedir 口径）。

### task-15 重测（2026-09-14，新增 faster-whisper 运行时）

| 项 | 值 |
|---|---|
| 总计 | **332,926,165 bytes（317.5 MiB）** |
| 较 task-10 基线 | **+132,865,740 bytes（+126.7 MiB）** |
| TOP1 `_internal/ctranslate2/ctranslate2.dll` | 56.55 MiB |
| TOP2 `interviewcopilot-sidecar.exe` | 31.44 MiB |
| TOP3 `_internal/numpy.libs/libscipy_openblas64…dll` | 19.64 MiB |
| TOP4 `_internal/av.libs/avcodec-62-…dll` | 18.37 MiB |
| TOP5 `_internal/onnxruntime/capi/*_pybind11_state.pyd` | 18.23 MiB |
| TOP6 `_internal/av.libs/libx265-…dll` | 12.16 MiB |

机器行（新基线）：`BUNDLE_BYTES=332926165`。

增量构成（collect 生效核对）：`ctranslate2` 39 项 **59.4 MiB**、`av`（含 `av.libs`
的 ffmpeg 系 DLL）**33.9 MiB**、`websockets` 72 项仍在（task-14 的 collect 未被破坏）。
**这两项 collect 必须留**：ctranslate2 的原生 DLL 按名字加载、`av` 是
`faster_whisper/audio.py` 的顶层 import，静态分析都看不到，漏了会「装得上但跑不了」。

**优化候选（记录，不做）**：`av.libs` 的 ffmpeg 系 DLL（avcodec/libx265 等 ~30 MiB）
本管线用不到——我们只把 numpy 数组喂给 `model.transcribe`，不经过 `decode_audio`。
裁掉需替换 `faster_whisper/audio.py` 的顶层 import 或做 exclude，列入后续体积优化。

> 口径注记：本次复测在**项目外临时目录**完成。原 workpath 无法重建——
> PyInstaller `--clean` 要清理 `build-sidecar/` 下 211 个中间文件，
> 被本机**批量删除守卫**（阈值 50 文件/回合）拦截，非沙箱层可解。
> 故 `dist-sidecar/` 内目前仍是 task-10 的旧包（190.8 MiB，**不含** ctranslate2/av）；
> 换机器或手动清空 `build-sidecar/` 后跑 `python sidecar/build.py` 即可复原标准路径。
> 两者的 gitignore 均生效，产物不入库。

> 路径注记（task-10）：sidecar 产物目录为 `dist-sidecar/`（构建目录 `build-sidecar/`）——
> 前端 vite 产物占用 `dist/` 且构建会清空它，实测一次误删 sidecar 包后分离。

### 基线沿革

- task-4：47,583,035 bytes（45.4 MiB）——embedding 栈（task-6/7）进包之前。
- task-10：200,060,425 bytes（190.8 MiB）——增量 ≈145MB 全部来自已立项范围：
  onnxruntime（~36MB）+ numpy/scipy-openblas（~35MB）+ transformers/tokenizers/
  huggingface-hub/hf-xet/safetensors 等（~40MB）+ 打包后 exe 内嵌代码（~25MB）。
  属范围驱动的台阶，非回归——基线据此重定，1.15× 继续看守未来回归。
  体积优化（裁下载期依赖、确认懒加载）列入后续，本任务只记录不做。
- task-15：332,926,165 bytes（317.5 MiB）——增量 +126.7 MiB 来自 faster-whisper
  运行时（ctranslate2 59.4 + av 33.9 + 附带 ~33），属已立项范围（本地 ASR），
  非回归。基线据此重定；**新基线 1.15× 上限 ≈ 383 MiB**，
  下个任务若再加体积需先核对是否越线。

### 自污染 bug（已修，教训）

- 现象：同一包连续两次测体积漂移（task-10 实测 200,059,722 → 200,290,258）。
- 根因：打包态 DB 默认路径落在 `_MEIPASS`（=`_internal/`）内，每次启动建库迁移，
  包自己弄脏自己；且只读安装（Program Files）下首写即崩。
- 修复：`default_db_path()` 打包态改走 OS 用户数据目录
 （Win `%LOCALAPPDATA%/InterviewCopilot/data`，单测锁定“不在 bundle 内”）；
  `default_model_dir()` 同理。实测：两次 `verify-packaged` 总字节完全一致，
  `_internal/data` 不存在，DB 落 `%LOCALAPPDATA%/InterviewCopilot/data/`。

## 启动（spawn → 可用，两次样本，cwd=System32）

| 样本 | spawn→握手 | spawn→/health 200 |
|---|---|---|
| 1（task-10） | 1.13s | 1.15s |
| 2（task-10） | 1.01s | 1.03s |
| 3（task-15，含 faster-whisper 运行时） | **2.79s** | **2.81s** |

对照 PRD §3.12（sidecar 后台预热 <5s，含拼音 ~500ms + 结巴词典 ~4s）：**通过**。
task-10 时余量很大（~1.0s）；**task-15 后涨到 2.8s，余量降到 ~2.2s**——
增量来自更大的 `_internal`（多出 ctranslate2/av 的 DLL 加载与目录扫描），
非权重加载（provider 是懒加载，启动路径不碰它）。仍达标，但这是后续加依赖时要盯的项。
差异说明：v0.7.1 的词典为懒加载——`jieba_dict()` 调用本身 ~0ms，
~0.5s 成本发生在首次中文查询（冒烟 jieba_query 首查 511.8ms），
不在启动路径上；PRD 的 ~4s 系旧版本行为假设。

## §3.13 四条校验（打包产物，cwd=System32，两次 ALL PASS）

1. bundle 内 vendor 文件逐个存在（无 datas 静默空目录）。
2. 任意 CWD 启动 → 合法握手 JSON（_MEIPASS 绝对锚定，绑定完成才打印）。
3. 打包件内 libsimple + dict 绝对路径加载 → 插入中文 →
   jieba_query / simple_query 命中 → rebuild（CWD 无关）。
4. `/health` → 200。

## 打包工程记录

- 工具：PyInstaller 6.22.3（Python 3.13.14，sqlite-vec 0.1.9，simple v0.7.1）。
- 实测坑：PyInstaller 6 不把脚本所在 `sidecar/src` 自动记入 pathex
  （`Analysis-00.toc` 实证），sibling 模块（app/core/database）被静默漏掉，
  产物启动即 `ModuleNotFoundError: No module named 'app'`——
  `build.py` 已用显式 `--paths sidecar/src` 修复；`verify-packaged.py`
  第 2 条（启动即验）是该类问题的永久兜底。
- `sqlite_vec/vec0.dll` 经 `--collect-all sqlite_vec` 收包（TOCs 实证为 BINARY）。

## 待办

- （已关闭，task-10）生产数据目录替代 `_internal/data`：见上节“自污染 bug”。
- 体积优化候选：裁剪下载期依赖（hf-xet/safetensors）、onnxruntime 精简——列入后续。

---

# ASR 时延实测（task-15，2026-09-14）

> 口径：**segment_end 发出 → asr_final 收到**，走**真实 uvicorn + 真实 WS 传输**
> （不是 `TestClient`——进程内客户端不反映真实链路）。VAD 判定本身在 Rust 侧
> （30ms/帧），不在本区间内。冷加载单列：它在 sidecar 启动预热路径上，
> 与「每段转写」是两笔账。

## 复现

```sh
python scripts/bench_asr_latency.py --durations 3,15 --repeat 2
```

## 结果

provider `faster-whisper` base / int8 / CPU；环境 Windows AMD64，Python 3.13.14。

| 项 | 值 |
|---|---|
| 冷加载（首次权重加载） | **2790 ms** |
| 3.0s 段（100 帧） | 中位 **1903 ms**（1896 / 1910） |
| 15.0s 段（500 帧） | 中位 **1888 ms**（1878 / 1899） |

段时长由 sidecar 按**帧数 × 30ms** 报回：实测 100 帧 → `duration_ms=3000`、
500 帧 → `duration_ms=15000`，与端点规格一致（±1 帧内）。

## 结论与影响

1. **冷加载 2790 ms**，与 task15 坑位预警的「首次 2–3s」吻合。故 provider 必须是
   **进程内单例**（`faster_whisper_provider._MODEL_CACHE`）；若每段重载，
   1.9s 的转写会变成 4.7s。
2. **转写耗时与段长几乎无关**（3s 与 15s 都在 ~1.9s）。原因：whisper 按 30s 窗口
   编码，≤30s 的段落在同一窗口内，算力是**常数级**而非线性。
   → 降级超时按「3× 段时长」派生（3s 段 9s、15s 段 45s），余量 4.7× / 23×。
   也说明**固定 10s 超时对本档模型并不危险**；但换成更大档位（small/medium）或
   长于 30s 的段后线性假设不成立，故仍按段时长派生，不写死。

## 未测（挂起项，不粉饰）

- **中文真人样本 → 文本正确（人耳比对）**：需真实录音，按「真实数据先跳过」挂起。
  本节音频为**合成信号**（谐波堆 + 音节包络）——足以驱动真实算力路径，故时延有效；
  但**转写文本无意义**，不得据此判断识别质量。
- 长段（>30s）与更大权重档位的时延：端点规格单段上限 15s，超出场景不在本轮范围。
- 双路并发（loopback + mic 同时说话）时延：本轮为单路串行口径。
