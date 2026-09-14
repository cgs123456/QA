# 实测档案：sidecar onedir 打包（task-4 建，task-10 重定基线，2026-09-14）

> 范围：sidecar-only 体积与启动。整包 Standard / Full 两档待 Tauri 壳联调后定（D4）。

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

> 路径注记（task-10）：sidecar 产物目录为 `dist-sidecar/`（构建目录 `build-sidecar/`）——
> 前端 vite 产物占用 `dist/` 且构建会清空它，实测一次误删 sidecar 包后分离。

### 基线沿革

- task-4：47,583,035 bytes（45.4 MiB）——embedding 栈（task-6/7）进包之前。
- task-10：200,060,425 bytes（190.8 MiB）——增量 ≈145MB 全部来自已立项范围：
  onnxruntime（~36MB）+ numpy/scipy-openblas（~35MB）+ transformers/tokenizers/
  huggingface-hub/hf-xet/safetensors 等（~40MB）+ 打包后 exe 内嵌代码（~25MB）。
  属范围驱动的台阶，非回归——基线据此重定，1.15× 继续看守未来回归。
  体积优化（裁下载期依赖、确认懒加载）列入后续，本任务只记录不做。

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
| 1 | 1.13s | 1.15s |
| 2 | 1.01s | 1.03s |

对照 PRD §3.12（sidecar 后台预热 <5s，含拼音 ~500ms + 结巴词典 ~4s）：**通过且余量大**。
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
