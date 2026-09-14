# M1（文本链路）验收报告（PRD §6.1，2026-09-14）

> 执行方式：本机可跑项全部实跑（命令 + 输出摘录见下）；需外部输入项如实标记。
> 结论：**13 通过 / 1 待定（外部阻塞）/ 1 部分通过（LLM 侧待 Ollama）/ 1 待人工走查**。
> 无失败项。`m1-text-pipeline` tag 暂缓，待三项外部条件清除（见 §4）。

## 1. 逐项验收（PRD §6.1 Phase 1a 验收标准表）

| # | 测试项（标准） | 结论 | 证据（命令 + 输出摘录） |
|---|---|---|---|
| 1 | simple 冒烟（五步） | 通过 | `pytest sidecar/tests/test_simple_extension.py -v` → 5 passed；`[simple-smoke-timings] load_extension=0.9ms, … jieba_query=511.8ms, simple_query=0.1ms` |
| 2 | Markdown 解析（100% 切分） | 通过 | `test_markdown_parser_rules` + `test_markdown_qa_blocks`：H1→entity/H2疑问→QA/陈述→字段首段/Q&A 块，合同样例全过 |
| 3 | JSON 解析（100% 导入） | 通过 | `test_json_parser_rules`：实体/qa_pairs/列表三形态 + 别名键透传，全过 |
| 4 | FTS5 中文 Top-3 >85% | 通过 | demo 集 17/17 = 100%（`evaluate` top3_rate=1.0）；见基线报告 |
| 5 | 拼音容错 Top-5 >75% | 通过 | demo 1/1 + 真机探针 `fahuo→eval-001 / tuihuo→eval-002 / kefu→eval-003` 全部 top-1 |
| 6 | 混合检索 100 题 Top-3 >85% | 待定（外部阻塞） | demo 代理 17/17 = 100%；100 题集待用户提供（task-5 约定），到货后重跑基线 |
| 7 | vec 事务一致性（中断 100% 干净） | 通过 | `test_sigkill_mid_compile_rolls_back`：4000 行编译中 TerminateProcess → `qa=0 vec=0 orphans=0`（断言非正常退出、无 Traceback，防假阳性） |
| 8 | SSE 两段式（流式正确） | 通过 | 真机：`SSE_ORDER=['retrieval','done']`；单测覆盖 chunk 透传顺序 decision→sources→chunk→done；禁缓冲头断言 |
| 9 | 固定答案 <200ms | 通过 | 真机 ask→done 全程 **2.4ms**（`DIRECT_ORDER=['retrieval','done']`） |
| 10 | 模糊检索 <5s（首字 <1s） | 部分通过 | 检索侧：ask→首个 retrieval 事件 **1.4ms**；LLM 首字待本机 Ollama（11434 拒连），`scripts/e2e_ollama.py` 已就绪转人工 |
| 11 | Fail-Closed（无匹配拒答） | 通过 | 真机无匹配题 → `知识库未命中`；单测空问/无匹配零 LLM 调用 |
| 12 | HTTP 鉴权（无 token →401） | 通过 | 真机：`POST /qa/ask → 401` / `POST /knowledge/compile → 401` / `GET /knowledge/search → 401` / `GET /health → 200` |
| 13 | 启动失败降级 UI | 待人工走查 | 链路单测全绿：版本 mismatch→事件（3 用例）、退避表、fallback 占位、stderr 落盘 + `sidecar_log_tail` + 重试命令；页面渲染需 Tauri 壳目检 |
| 14 | 知识库切换（is_current + 检索跟随） | 通过 | 单测切库跟随（B 有命中→A 无命中）+ Playwright HTTP 级复现通过 |
| 15 | 注入防护（不执行） | 通过 | 6 用例全绿：结构隔离（标签计数）+ 伪造闭合转义 + 问题恒末位 |
| 16 | onedir 体积（落盘） | 通过 | `docs/benchmark.md`：200,060,425 bytes（190.8 MiB，两次一致）+ `check_size.py` PASS |

全量回归（本轮复跑）：pytest **104 passed**、vitest 9、Playwright 1（4.5s）、tsc/eslint 0。

## 2. 特别复核 A：三条结构性融合案例（精确值）

`test_hybrid_rank.py` 7 passed + 独立算术复核：

```
仅字段命中      = 3.0/6.0               = 0.5   ✓（== 0.5 精确断言）
三路模糊全满分  = (1.0+1.0+1.0)/6.0     = 0.5   ✓（== 0.5 精确断言）
字段+三路门限   = (3.0+0.5+0.5+0.5)/6.0 = 0.75  ✓（== 0.75 精确断言，top1 为 qa 候选）
全分 ∈[0,1]（越界钳入） + decide 六边界 ✓
```

记录在案的算术注记（引擎未动）：向量路门限 s=0.7，若三路均取各自门限 s 则为
(3+0.5+0.5+0.7)/6≈0.783；此处按 PRD 字面 (3.0+1.5)/6.0=0.750 锚定，留待标定。

## 3. 特别复核 B：FTS「有数据状态下」全部验证清单

空表 MATCH 是假绿——本轮确认每一处 FTS 断言都先插数据：

- `test_simple_extension.py`：插入“你的发货周期是多久”后双查询 + `test_rerun_with_data_present`
  （断言 `COUNT>0` 才重跑 MATCH / `SELECT *` / `rebuild`）——5 passed。
- `test_migrations.py`：触发器 ai/au/ad 均先 seed 后 MATCH（正/反断言：update 后旧词查无、新词命中；
  delete 后查无）——8 passed（含 CHECK 约束、幂等、vec 可写）。
- `test_retrieval.py`：seed（含 1 条 rejected）后断言中文/拼音命中、rejected 双路排除、top_k——6 passed。
- `test_qa_stream.py` / Playwright：全经 HTTP compile 真实入库后检索——通过。
- 结论：无一处空表验证；R4 遵守。

## 4. Tag 判定：暂缓 `m1-text-pipeline`

无失败项，但三项非引擎阻塞未清除，打 tag 会夸大完备性，故暂缓：

| 阻塞项 | 性质 | 清除条件 | 清除动作 |
|---|---|---|---|
| #6 100 题集 | 外部数据（用户提供） | 用户给出 100 题 | 重跑基线 → 更新本报告 → 打 tag |
| #10 LLM 首字 | 环境（本机无 Ollama） | Ollama 可用 | 跑 `scripts/e2e_ollama.py` 补首字数字 |
| #13 降级页目检 | 环境（无工具链/壳） | 有壳机器 | 按 PROGRESS 走查脚本目检降级页 + kill×4 |

三项已记入 `docs/PROGRESS.md` 待办。用户可明确指示“按当前范围打 tag”以覆盖本判定。

## 5. 基线快照（阈值全默认，未改）

demo 8QA/20 题：Top-3/5=1.0；direct=0.15；Fail-Closed=0.80；null 题 FC=1.0；
延迟 field 0.12 / fts 29.9 / vec 2.8 / embed 2.4 / fuse 0.03ms。详见 `docs/eval-baseline.md`
（含解读：精确相等门槛 + 纯模糊上限 0.5 的结构性后果；标定纪律：不动权重/阈值）。
