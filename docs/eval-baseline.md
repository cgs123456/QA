# 评测基线（四路 + 融合 + 判定，2026-09-14）

> D6 前基线：阈值一律默认值，未改。调整按 PRD 标定顺序（s_i→w_i→阈值）并记录。

## 语料与模型

- 种子：tests/eval/seed_demo.json（8 QA + 4 字段；QA 均带 category=公司信息，还原 Markdown 形态）
- 题目：tests/eval/questions_100.jsonl（59 题：49 scored + 10 应 Fail-Closed；
  其中 17+3 为早期关键词示例，32+7 为本轮补的自然真题 tags=real；冻结前仍为示例集，
  100 题目标待继续扩充）
- embedding：bge-small-zh-v1.5 (ONNX fp32, onnx-community)（base BAAI/bge-small-zh-v1.5，dim 512，L2 归一化，事务外批量）
- 入库统计：qa_inserted=8 fields_upserted=4 vec_written=8

## 阈值（默认）

| 常量 | 值 |
|---|---|
| TH_DIRECT | 0.75 |
| TH_MAYBE | 0.45 |
| GAP | 0.15 |
| bm25 门限 | -0.5（→s=0.5，T_fts=0.5） |
| vec 门限 | d<0.6（→s=0.7） |
| w | field:3, jieba:1, simple:1, vec:1（Σ=6.0） |

## 基线数字

- Top-3：0.490；Top-5：0.490（49 scored）
- 直接返回率：0.051；Fail-Closed 率：0.932
- null 题 Fail-Closed 率：1.000（10 题）
- 动作分布：{'maybe_single': 1, 'fail_closed': 55, 'direct': 3}
- 各路平均延迟（ms）：field=0.11；fts=9.44；vec=2.53；embed=2.44；fuse=0.02

## 逐题

| 题目 | 期望 | top1 | 动作 | top3 |
|---|---|---|---|---|
| 发货周期多久 | eval-001 | qa:eval-001@0.456 | maybe_single | True |
| 三天发货 | eval-001 | qa:eval-001@0.293 | fail_closed | True |
| 退货期限 | eval-002 | qa:eval-002@0.920 | direct | True |
| 几天无理由退货 | eval-002 | qa:eval-002@0.313 | fail_closed | True |
| 客服电话 | eval-003 | qa:eval-003@0.919 | direct | True |
| 客服工作时间 | eval-003 | qa:eval-003@0.302 | fail_closed | True |
| 公司成立哪一年 | eval-004 | qa:eval-004@0.448 | fail_closed | True |
| 公司成立时间 | eval-004 | qa:eval-004@0.811 | direct | True |
| 微信付款 | eval-005 | qa:eval-005@0.302 | fail_closed | True |
| 支付宝支持 | eval-005 | qa:eval-005@0.309 | fail_closed | True |
| 发票怎么开 | eval-006 | qa:eval-006@0.448 | fail_closed | True |
| 电子发票 | eval-006 | qa:eval-006@0.300 | fail_closed | True |
| 优惠券叠加 | eval-007 | qa:eval-007@0.434 | fail_closed | True |
| 优惠券每单限用 | eval-007 | qa:eval-007@0.313 | fail_closed | True |
| 地址填错 | eval-008 | qa:eval-008@0.420 | fail_closed | True |
| 收货地址修改 | eval-008 | qa:eval-008@0.310 | fail_closed | True |
| fahuo | eval-001 | qa:eval-001@0.245 | fail_closed | True |
| 今天天气怎么样？ | null(应拒答) | - | fail_closed | - |
| 讲个笑话吧 | null(应拒答) | - | fail_closed | - |
| 量子力学是什么？ | null(应拒答) | - | fail_closed | - |
| 你们多久能发货啊？ | eval-001 | - | fail_closed | False |
| 我下单之后要等几天才发货？ | eval-001 | - | fail_closed | False |
| 发货快不快？ | eval-001 | - | fail_closed | False |
| 请问发货周期是多长时间？ | eval-001 | qa:eval-001@0.139 | fail_closed | True |
| 不想要了可以退吗？ | eval-002 | - | fail_closed | False |
| 退货的话运费谁出？ | eval-002 | - | fail_closed | False |
| 七天无理由是真的吗？ | eval-002 | - | fail_closed | False |
| 请问退货期限是多久？ | eval-002 | qa:eval-002@0.132 | fail_closed | True |
| 客服电话给我一下 | eval-003 | - | fail_closed | False |
| 人工客服的工作时间是？ | eval-003 | - | fail_closed | False |
| 有400电话吗？ | eval-003 | - | fail_closed | False |
| 怎么打你们客服？ | eval-003 | - | fail_closed | False |
| 你们公司成立多少年了？ | eval-004 | - | fail_closed | False |
| 公司总部在哪？ | eval-004 | - | fail_closed | False |
| 这家公司是哪年创办的？ | eval-004 | - | fail_closed | False |
| 公司成立时间？ | eval-004 | qa:eval-004@0.429 | fail_closed | True |
| 平时用微信支付行不行 | eval-005 | - | fail_closed | False |
| 支付宝付款支持吗 | eval-005 | - | fail_closed | False |
| 可以用微信付款吗 | eval-005 | - | fail_closed | False |
| 支持支付宝吗 | eval-005 | - | fail_closed | False |
| 能开发票吗 | eval-006 | - | fail_closed | False |
| 发票抬头怎么填 | eval-006 | - | fail_closed | False |
| 开票需要备注什么 | eval-006 | - | fail_closed | False |
| 怎么开电子发票 | eval-006 | qa:eval-006@0.313 | fail_closed | True |
| 两张券能一起用吗 | eval-007 | - | fail_closed | False |
| 一单可以用几张优惠券 | eval-007 | - | fail_closed | False |
| 优惠券有使用限制吗 | eval-007 | - | fail_closed | False |
| 优惠券能叠加吗 | eval-007 | qa:eval-007@0.144 | fail_closed | True |
| 地址写错了还能改吗 | eval-008 | - | fail_closed | False |
| 怎么修改收货地址 | eval-008 | qa:eval-008@0.314 | fail_closed | True |
| 发货了才发现地址错了怎么办 | eval-008 | qa:eval-008@0.117 | fail_closed | True |
| 改地址找谁 | eval-008 | - | fail_closed | False |
| 今天适合出门吗 | null(应拒答) | - | fail_closed | - |
| 帮我写一封辞职信 | null(应拒答) | - | fail_closed | - |
| 你们支持货到付款吗 | null(应拒答) | - | fail_closed | - |
| 有纸质发票吗 | null(应拒答) | - | fail_closed | - |
| 附近有什么好吃的 | null(应拒答) | - | fail_closed | - |
| 讲个恐怖故事 | null(应拒答) | - | fail_closed | - |
| 你们老板是谁 | null(应拒答) | - | fail_closed | - |

（tmp 库已弃；全量耗时见运行输出）

## 基线解读（阈值未改，只记录）

1. **Top-3 = 0.490（24/49），未达 85% 线**：自然问法下 FTS AND 语义大量落空
   （虚词/换说法任一分词缺失即整路归零，25 个 miss 全部返回空池），向量路在 8 文档
   小库上也救不全。这是抽检确认的真实缺口，不是调参能“修”的——需 D6 的查询关键词
   提取 + 真实规模语料重标定（标定顺序 s_i→w_i→阈值）。
2. **零答错**：25 个 miss 全部是空池拒答，无一错答（top1 错误 0 例）；
   另有 7 题 rank-1 命中但分值低于 0.45 而拒答（如“请问发货周期是多长时间？” rank-1
   仅 0.139）——宁可错杀的 Fail-Closed 偏向符合 PRD 红线，但阈值松紧留待标定。
3. **null 题 Fail-Closed 率 1.000（10/10）**：含对抗性 null（纸质发票/货到付款），
   PRD 红线守住。
4. **延迟**：field 0.11 / fts 9.44 / vec 2.53 / embed 2.44 / fuse 0.02ms，均远低于预算。
5. 早期 20 题 demo 基线（Top-3=1.0）为关键词自测，不再作为达标证据，留档备查。

## v2（匹配根因修复后基线，2026-09-15）

> 本轮只改**匹配**：查询关键词化（FTS 由 AND 改关键词 OR）、字段直查增加受控词表内的包含匹配。**阈值一个都没动**（R15）：TH_DIRECT/TH_MAYBE/GAP、bm25 门限、vec 门限、w 全部沿用默认值。

### 与 v1 对比（同一套仪器）

| 指标 | v1 | v2 | Δ |
|---|---|---|---|
| Top-3 | 0.490 | 0.980 | +0.490 |
| Top-5 | 0.490 | 0.980 | +0.490 |
| 字段命中率 | 0.061 | 0.449 | +0.388 |
| Fail-Closed 率 | 0.932 | 0.610 | -0.322 |
| 直接返回率 | 0.051 | 0.254 | +0.203 |
| null 题 Fail-Closed 率 | 1.000 | 1.000 | +0.000 |

**PRD 红线（答错）**：只有真的给出答案才可能答错，拒答是安全侧、不计入分母。v1 答了 4 题（3 direct + 1 maybe_single），逐题表里 top1 全部等于期望 → 答错 **0**；v2 答了 **23** 题、答错 **1** 题（答错率 0.043）。

### 对 v1 节「零答错」的更正

v1 节写着「零答错：25 个 miss 全部是空池拒答，无一错答」。按 v1 当时的输出，这句话**字面成立**（答了 4 题、全对）——但不能读成「匹配质量好」：v1 只答了 4/49，其余 45 题压根没进打分环节（AND 语义整路归零 → 空池拒答）。
把召回修好之后，**同一套题目**立刻暴露出 1 例错答（见下节）。也就是说 v1 的「零错答」是**低召回的副产品**，不是安全设计的结果：PRD 红线在 v1 上并没有被验证过，只是没有被触发。

（历史节不改：v1 节原样保留，更正记在这里，两处数字都可回溯到各自的逐题表。）

### 已知残留（1 例，根因已定位）

`我下单之后要等几天才发货？`（期望 eval-001，实得 **eval-006 @0.628**，动作 maybe_multi）。

关键词 `下单 / 之后 / 几天 / 发货` 在 8 篇 QA 里各投一票：`下单`→eval-006、`几天`→eval-002、`发货`→eval-001 与 eval-008。四篇的 jieba bm25 落在 −1.25 ~ −1.64，字段路对四篇一视同仁（都联动 s=1.0、不区分），融合分因此挤成 **0.628 / 0.627 / 0.623 / 0.619（极差 0.009）**；top1 落在 eval-006 只是因为它那一票最强（bm25 最负）。
**这是词袋检索在 8 文档语料上的固有歧义，不是实现缺陷**：极差 0.009 远够不上 GAP=0.15，系统判 `maybe_multi`（交用户判断），正是 PRD 期望的保守行为 —— 它没有「自信地答错」。
v1 之所以「安全」，是因为 AND 语义让这条路整体落空 → 空池拒答；OR 化把召回换来了，顺带暴露了这个歧义。**没有用启发式去「修」它**（例如给疑问词位置加权、给短词降权），那属于标定阶段的事（R15）。

### 被否决的变体（记录在案）

把字符级 OR 从「仅 ≤2 关键词」扩到「所有中文查询」（simple 路恒为字符 OR）后实测：
Top-3 **1.000**、direct 0.356、null 仍 10/10 —— 但同一道残留题的 top1 从`maybe_multi` 档的 eval-006@0.628 变成 **eval-002@0.773**，越过 TH_DIRECT=0.75 → **`direct`（自信地答错）**。
按 PRD「宁可错杀」取向，取 Top-3 低 0.02 但错答停在「交用户判断」档的实现。（判定对 ≥0.75 是短路、不看 GAP，所以 0.773 必然进 direct 档。）
（另：中文查询下 `simple_query(raw)` 是字符级 AND，恒为 `[]`，故「字符 OR ⊇ 字符 AND」，本实现与「并集」语义等价。）

### 语料规模效应（必读）

- 本轮语料 **qa_docs = 8**，题目 59 题（49 scored + 10 应拒答）。
- FTS5 的 idf = `ln((N-n+0.5)/(n+0.5))`，N 小则 idf 塌缩、bm25 量级随之变小；**Top-3 这类召回指标在 8 文档语料上不可外推**。
- 实测：本语料下真实命中的 bm25 落在 −1.2 ~ −6.0，仍远低于 −0.5 门限，即**本轮的门限没有被小语料卡住**；小语料真正影响的是 OR 化后同池文档数变多、idf 下降，导致单题绝对分整体下移（相对排序才是本轮的收益来源）。

### 语料与模型

- 种子：sidecar/tests/eval/seed_demo.json（8 QA + 4 字段；QA 均带 category=公司信息，还原 Markdown 形态）
- 题目：sidecar/tests/eval/questions_100.jsonl（59 题：49 scored + 10 应 Fail-Closed）
- embedding：bge-small-zh-v1.5 (ONNX fp32, onnx-community)（base BAAI/bge-small-zh-v1.5，dim 512，L2 归一化，事务外批量）
- 入库统计：qa_inserted=8 fields_upserted=4 vec_written=8

### 阈值（默认）

| 常量 | 值 |
|---|---|
| TH_DIRECT | 0.75 |
| TH_MAYBE | 0.45 |
| GAP | 0.15 |
| bm25 门限 | -0.5（→s=0.5，T_fts=0.5） |
| vec 门限 | d<0.6（→s=0.7） |
| w | field:3, jieba:1, simple:1, vec:1（Σ=6.0） |

### 数字

- qa_docs：8；scored：49；null：10
- Top-3：0.980；Top-5：0.980
- 字段命中率：0.449
- 答了 23 题、答错 1 题（答错率 0.043）
- 直接返回率：0.254；Fail-Closed 率：0.610
- null 题 Fail-Closed 率：1.000（10 题）
- 动作分布：{'direct': 15, 'fail_closed': 36, 'maybe_multi': 4, 'maybe_single': 4}
- 各路平均延迟（ms）：prep=9.42；field=0.25；fts=0.57；vec=2.67；embed=2.63；fuse=0.04
- 全量耗时：0.9s

### 逐题

| 题目 | 期望 | top1 | 动作 | top3 |
|---|---|---|---|---|
| 发货周期多久 | eval-001 | qa:eval-001@0.956 | direct | True |
| 三天发货 | eval-001 | qa:eval-001@0.793 | direct | True |
| 退货期限 | eval-002 | qa:eval-002@0.920 | direct | True |
| 几天无理由退货 | eval-002 | qa:eval-002@0.811 | direct | True |
| 客服电话 | eval-003 | qa:eval-003@0.919 | direct | True |
| 客服工作时间 | eval-003 | qa:eval-003@0.802 | direct | True |
| 公司成立哪一年 | eval-004 | qa:eval-004@0.948 | direct | True |
| 公司成立时间 | eval-004 | qa:eval-004@0.811 | direct | True |
| 微信付款 | eval-005 | qa:eval-005@0.302 | fail_closed | True |
| 支付宝支持 | eval-005 | qa:eval-005@0.309 | fail_closed | True |
| 发票怎么开 | eval-006 | qa:eval-006@0.445 | fail_closed | True |
| 电子发票 | eval-006 | qa:eval-006@0.300 | fail_closed | True |
| 优惠券叠加 | eval-007 | qa:eval-007@0.434 | fail_closed | True |
| 优惠券每单限用 | eval-007 | qa:eval-007@0.308 | fail_closed | True |
| 地址填错 | eval-008 | qa:eval-008@0.420 | fail_closed | True |
| 收货地址修改 | eval-008 | qa:eval-008@0.310 | fail_closed | True |
| fahuo | eval-001 | qa:eval-001@0.245 | fail_closed | True |
| 今天天气怎么样？ | null(应拒答) | - | fail_closed | - |
| 讲个笑话吧 | null(应拒答) | - | fail_closed | - |
| 量子力学是什么？ | null(应拒答) | - | fail_closed | - |
| 你们多久能发货啊？ | eval-001 | qa:eval-001@0.793 | direct | True |
| 我下单之后要等几天才发货？ | eval-001 | qa:eval-006@0.628 | maybe_multi | True |
| 发货快不快？ | eval-001 | qa:eval-001@0.623 | maybe_multi | True |
| 请问发货周期是多长时间？ | eval-001 | qa:eval-001@0.786 | direct | True |
| 不想要了可以退吗？ | eval-002 | qa:eval-007@0.154 | fail_closed | True |
| 退货的话运费谁出？ | eval-002 | qa:eval-002@0.647 | maybe_single | True |
| 七天无理由是真的吗？ | eval-002 | qa:eval-002@0.144 | fail_closed | True |
| 请问退货期限是多久？ | eval-002 | qa:eval-002@0.779 | direct | True |
| 客服电话给我一下 | eval-003 | qa:eval-003@0.645 | maybe_multi | True |
| 人工客服的工作时间是？ | eval-003 | qa:eval-003@0.646 | maybe_multi | True |
| 有400电话吗？ | eval-003 | qa:eval-003@0.797 | direct | True |
| 怎么打你们客服？ | eval-003 | qa:eval-003@0.758 | direct | True |
| 你们公司成立多少年了？ | eval-004 | qa:eval-004@0.650 | maybe_single | True |
| 公司总部在哪？ | eval-004 | qa:eval-004@0.158 | fail_closed | True |
| 这家公司是哪年创办的？ | eval-004 | qa:eval-004@0.650 | maybe_single | True |
| 公司成立时间？ | eval-004 | qa:eval-004@0.929 | direct | True |
| 平时用微信支付行不行 | eval-005 | qa:eval-005@0.149 | fail_closed | True |
| 支付宝付款支持吗 | eval-005 | qa:eval-005@0.153 | fail_closed | True |
| 可以用微信付款吗 | eval-005 | qa:eval-005@0.302 | fail_closed | True |
| 支持支付宝吗 | eval-005 | qa:eval-005@0.307 | fail_closed | True |
| 能开发票吗 | eval-006 | qa:eval-006@0.150 | fail_closed | True |
| 发票抬头怎么填 | eval-006 | qa:eval-006@0.148 | fail_closed | True |
| 开票需要备注什么 | eval-006 | qa:eval-006@0.128 | fail_closed | True |
| 怎么开电子发票 | eval-006 | qa:eval-006@0.311 | fail_closed | True |
| 两张券能一起用吗 | eval-007 | - | fail_closed | False |
| 一单可以用几张优惠券 | eval-007 | qa:eval-007@0.136 | fail_closed | True |
| 优惠券有使用限制吗 | eval-007 | qa:eval-007@0.147 | fail_closed | True |
| 优惠券能叠加吗 | eval-007 | qa:eval-007@0.454 | maybe_single | True |
| 地址写错了还能改吗 | eval-008 | qa:eval-008@0.149 | fail_closed | True |
| 怎么修改收货地址 | eval-008 | qa:eval-008@0.312 | fail_closed | True |
| 发货了才发现地址错了怎么办 | eval-008 | qa:eval-008@0.768 | direct | True |
| 改地址找谁 | eval-008 | qa:eval-008@0.149 | fail_closed | True |
| 今天适合出门吗 | null(应拒答) | - | fail_closed | - |
| 帮我写一封辞职信 | null(应拒答) | - | fail_closed | - |
| 你们支持货到付款吗 | null(应拒答) | - | fail_closed | - |
| 有纸质发票吗 | null(应拒答) | - | fail_closed | - |
| 附近有什么好吃的 | null(应拒答) | - | fail_closed | - |
| 讲个恐怖故事 | null(应拒答) | - | fail_closed | - |
| 你们老板是谁 | null(应拒答) | - | fail_closed | - |

（tmp 库为一次性临时库，跑完即弃；完整机器可读摘要见 docs/eval-v2-summary.json）

