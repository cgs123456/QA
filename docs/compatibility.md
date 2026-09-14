# 兼容性报告：simple 扩展（P0 冒烟）

> 状态：**PASS（2026-09-14）**——五步全绿，全项目唯一 P0 风险关闭。

## 来源（可复现）

| 项 | 值 |
|---|---|
| 上游 | `wangfenjin/simple` **v0.7.1**（Latest，2026-02-23，commit `4ed0089`） |
| 变体 | `libsimple-windows-x64.zip`（5,473,005 bytes，本机 AMD64） |
| URL | `https://github.com/wangfenjin/simple/releases/download/v0.7.1/libsimple-windows-x64.zip` |
| sha256 | `7f03cc28cf307721f5621b5a52ef3bcb26c5215de012b09900492eb34d5bed0b`（下载后实测一致，尺寸一致） |
| 许可 | MIT/GPL-3.0 双许可，按 MIT 使用（R16 已消除） |
| 落盘 | zip 内 `simple.dll` → `sidecar/vendor/libsimple.dll`（改名，按路径加载）；dict 五文件 → `sidecar/vendor/dict/`（gitignored，本地专用） |
| 词典来源 | CppJieba 词典（zip 内 `dict/README.md`）；v0.7.1 更新 pinyin.txt（#202） |

## 环境

| 项 | 值 |
|---|---|
| Python | 3.13.14（MSC v.1944 64-bit AMD64，官方构建，`enable_load_extension` 可用） |
| `sqlite3.sqlite_version` | 3.53.1 |
| 扩展本体 | 1,518,592 bytes（~1.5MB，符合 PRD ~1-2MB 预期） |

## 五步实测（`test_simple_extension.py`，`[simple-smoke-timings]` 原样）

| 步骤 | 耗时 | 说明 |
|---|---|---|
| ① load_extension | 0.9ms | 符合 PRD「扩展本体 <2ms」 |
| ② jieba_dict | ~0.0ms | 该调用仅登记路径；实际词典在首次查询时懒加载（见下） |
| ③ 建表 FTS5 simple | 0.3ms | |
| ④ 插入中文 | 44.6ms | 含 simple 分词索引 |
| ⑤ jieba_query（发货周期） | 511.8ms | 首次查询，含词典/拼音懒加载；符合 PRD「拼音 ~500ms」量级 |
| ⑤ simple_query（fahuo） | 0.1ms | 热查询，拼音容错命中 |
| ⑤ 追加（有数据重跑 MATCH / SELECT * / rebuild） | 通过 | rebuild 后双路仍命中 |

## 警告输出（重要，已实测）

1. **`idf.utf8` 缺失 = 进程 abort，不可捕获**：`jieba_query` 路经
   `KeywordExtractor` 需要 `dict/idf.utf8`；缺失时扩展打印
   `FATAL ... open .../idf.utf8 failed` 并调用 `abort()`，
   整个 Python 进程 `Fatal Python error: Aborted`（不是可捕获的
   `sqlite3.Error`）。结论：
   - PRD §3.13「idf.utf8 仅 TF-IDF 时需要」对 v0.7.1 的 `jieba_query` 不成立——
     **dict 五文件必须齐全**（jieba.dict.utf8 / hmm_model.utf8 / user.dict.utf8 /
     idf.utf8 / stop_words.utf8）。
   - sidecar 启动自检必须在对外服务前校验 dict 完整性（`self_test.py` 待办），
     否则一次中文查询就能带崩整个 sidecar 进程（含 R7 重启循环也救不了缺文件）。
2. 本次无 SQLite 版本差异 / 变体错误：`load_extension` 一次成功，无警告。
3. `jieba_dict` 只作用于当前连接：冒烟全文件共用单连接（实现见测试 fixture）。
