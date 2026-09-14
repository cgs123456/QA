# 实测档案：sidecar onedir 打包（task-4，2026-09-14）

> 范围：sidecar-only 体积与启动。整包 Standard / Full 两档待 Tauri 壳联调后定（D4）。

## 方法

```sh
python sidecar/build.py                       # PyInstaller onedir
python scripts/verify-packaged.py             # §3.13 四条校验（cwd=C:\Windows\System32）
```

## 体积（onedir 总和，两次一致）

| 项 | 值 |
|---|---|
| `dist/interviewcopilot-sidecar` 总计 | **47,583,035 bytes（45.4 MiB）** |
| TOP1 `_internal/libcrypto-3-x64.dll` | 7.61 MiB |
| TOP2 `interviewcopilot-sidecar.exe` | 6.05 MiB |
| TOP3 `_internal/vendor/dict/idf.utf8` | 5.97 MiB |
| TOP4 `_internal/python313.dll` | 5.86 MiB |
| TOP5 `_internal/vendor/dict/jieba.dict.utf8` | 5.17 MiB |

词典三巨头（idf + jieba.dict + hmm ≈ 11.7MB）占包 ~26%。

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

- 生产数据目录（AppData）替代 `_internal/data`：联调任务处理，本任务 DB 路径
  按 BASE 锚定可验证即可。
