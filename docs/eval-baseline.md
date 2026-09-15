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

## v3（真实规模语料后基线，2026-09-15）

> 本轮**只动语料与题目，引擎一行没改**：阈值（TH_DIRECT/TH_MAYBE/GAP、bm25 门限、vec 门限）与权重 w 全部沿用 v2，匹配逻辑未动。

### 先说清楚：v2 ↔ v3 的汇总数字**不可比**

v2 是「8 篇文档里找 1 篇」，v3 是「108 篇文档里找 1 篇」——**这是两台仪器**，而且分母也变了（49 → 87 scored）。汇总列的差值没有「变好 / 变坏」的含义。要量语料规模的影响，只能拿**同一批题**在新旧语料下比：

### 旧 59 题：同一批题、两种语料规模

| 指标 | 旧 59 题 @ 8 文档（v2） | 旧 59 题 @ 108 文档（v3） | Δ |
|---|---|---|---|
| Top-3 | 0.980 | 0.796 | -0.184 |
| Top-5 | 0.980 | 0.939 | -0.041 |
| 答了（题） | 23 | 30 | +7 |
| 答错（题） | 1 | 11 | +10 |
| null 题 Fail-Closed 率 | 1.000 | 0.900 | -0.100 |

（口径：两列同一份 `evaluate_full`、同一组默认阈值。左列即 v2 节的汇总数字——v2 的题目集本来就是这 59 题；右列是从本轮 details 前缀切出的子集。题目顺序 = 文件顺序，故「前 59 行」就是旧集。）

### 语料规模效应（本轮实测）

- 语料：qa_docs = **108**（v2 为 8）；题目 100 题（87 scored + 13 应拒答）。
- FTS5 的 idf = `ln((N-n+0.5)/(n+0.5))`：单词命中在 N=8 时 idf=ln(5.0)≈1.61，N=108 时 idf=ln(107.5/1.5)≈4.27。**idf 塌缩在 8 文档语料上是结构性的**，本轮扩库后该问题消除，Top-3 才具备可外推性。
- 108 篇语料中 **62 篇没有对应题目**，是纯干扰项（真实知识库里绝大多数条目不会被问到）。v2 的 8 篇里没有这种干扰项，这是两个基线不可比的第二个原因。

### 已知缺口（本轮暴露；属 D6 查询理解 / 标定范围，**本轮一例未修**）

v2 的「Top-3 0.980 / 答错 1 例 / null 全拒答」在 108 篇语料上**没有被复现**：87 scored 题里答了 59 题、**答错 18 题**（答错率 0.305），其中 **direct 档错答 5 例**（自信地答错，PRD 红线）；13 道应拒答题里 **2 道未拒答**。

#### 1) direct 档错答（最严重：用户看到的是「确定答案」）

| 问题 | 期望 | 实得 | 分 |
|---|---|---|---|
| 发票怎么开 | eval-006 | eval-055 | 0.812 |
| 电子发票 | eval-006 | eval-058 | 0.816 |
| 运费怎么收的 | eval-012 | eval-038 | 0.800 |
| 退款是退到哪个账户 | eval-034 | eval-043 | 0.782 |
| 保修期多久 | eval-093 | eval-001 | 0.797 |

#### 2) 其余错答（maybe_* 档，交用户判断）

| 问题 | 期望 | 实得 | 分 | 动作 |
|---|---|---|---|---|
| 我下单之后要等几天才发货？ | eval-001 | eval-002 | 0.647 | maybe_multi |
| 发货快不快？ | eval-001 | eval-011 | 0.654 | maybe_multi |
| 退货的话运费谁出？ | eval-002 | eval-038 | 0.656 | maybe_multi |
| 七天无理由是真的吗？ | eval-002 | eval-039 | 0.646 | maybe_multi |
| 怎么打你们客服？ | eval-003 | eval-008 | 0.655 | maybe_multi |
| 平时用微信支付行不行 | eval-005 | eval-049 | 0.645 | maybe_multi |
| 发票抬头怎么填 | eval-006 | eval-057 | 0.658 | maybe_multi |
| 开票需要备注什么 | eval-006 | eval-068 | 0.654 | maybe_multi |
| 怎么开电子发票 | eval-006 | eval-058 | 0.656 | maybe_multi |
| 你们一般发哪家快递 | eval-010 | eval-011 | 0.653 | maybe_multi |
| 新疆能发货吗 | eval-013 | eval-001 | 0.645 | maybe_multi |
| 发货之后大概几天能到 | eval-015 | eval-002 | 0.647 | maybe_multi |
| 可以加急发货吗 | eval-020 | eval-007 | 0.656 | maybe_multi |

#### 3) 应拒答却给了动作

| 问题 | 动作 |
|---|---|
| 有纸质发票吗 | direct |
| 帮我翻译一下这句话 | maybe_single |

#### 机制（已用四路探针逐条定位，可复现）

① **字段路的 entity 级联动在真实规模语料下失去区分度。**字段命中会把**同 entity 的全部 QA** 拉到 `s.field=1.0`（不是命中字段本身的 0.8），于是「支付与发票」的 20 篇一律拿满 +3.0/6.0。8 篇语料时这不是问题（同 entity 只有 8 篇、且各题用词不重）；20 篇同义词群时排序退化为 bm25 噪声：`发票怎么开` 的 top3 挤在 **0.812 / 0.809 / 0.809（极差 0.003）**，越过 TH_DIRECT=0.75 直接进 direct 档。

② **包含匹配对「通用疑问词」没有防线。**`field_vocab.json` 里 `发货周期` 的别名含 `多久发货`，而 `instr(alias, kw)` 是子串判定 —— 关键词 `多久` 因此命中了 `发货周期` 字段。后果是跨 entity 污染：`保修期多久`（期望 eval-093，产品与规格）被 `发货周期`→eval-001（公司信息）拉成 **direct @0.797**。`field_lookup` 的注释已用「词表里没有 `支持`」解释过null 题为何安全，但没有覆盖「词表里的别名**含有**通用疑问词」这一情形。

③ **对抗性 null 的有效期与语料规模绑定。**`有纸质发票吗` 在 8 篇语料下拒答（v2 有记录），在 108 篇的发票题群下变成 **direct** —— 词袋无法区分「问纸质发票是否存在」与「问发票怎么开」。本轮新增的两道对抗 null（`你们有实体店吗` / `你们有微信公众号吗`）都正常拒答，说明问题不在「对抗性」而在「语料里出现了同词族的答案」。

**纪律**：以上三条都落在 R15 的标定范围内（查询理解 / 匹配规则 / 阈值），本轮**没有动引擎一行** —— 阈值、权重、匹配逻辑全部等于 v2。把它们留给 D6，而不是为了把本轮数字做好看而当场调参。

### 语料与模型

- 种子：sidecar/tests/eval/seed_demo.json（108 QA + 29 字段；类目 6 个：公司信息、物流配送、退换货、支付与发票、会员与优惠、产品与规格）
- 题目：sidecar/tests/eval/questions_100.jsonl（100 题：87 scored + 13 应 Fail-Closed）
- embedding：bge-small-zh-v1.5 (ONNX fp32, onnx-community)（base BAAI/bge-small-zh-v1.5，dim 512，L2 归一化，事务外批量）
- 入库统计：qa_inserted=108 fields_upserted=29 vec_written=108

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

- qa_docs：108；scored：87；null：13
- Top-3：0.862；Top-5：0.966
- 字段命中率：0.632
- 答了 59 题、答错 18 题（答错率 0.305）
- 直接返回率：0.370；Fail-Closed 率：0.390
- null 题 Fail-Closed 率：0.846（13 题）
- 动作分布：{'direct': 37, 'fail_closed': 39, 'maybe_multi': 14, 'maybe_single': 10}
- 各路平均延迟（ms）：prep=5.90；field=0.32；fts=1.04；vec=3.15；embed=2.74；fuse=0.05
- 全量耗时：1.3s

### 逐题

| 题目 | 期望 | top1 | 动作 | top3 |
|---|---|---|---|---|
| 发货周期多久 | eval-001 | qa:eval-001@0.969 | direct | True |
| 三天发货 | eval-001 | qa:eval-001@0.816 | direct | True |
| 退货期限 | eval-002 | qa:eval-002@0.935 | direct | True |
| 几天无理由退货 | eval-002 | qa:eval-002@0.820 | direct | True |
| 客服电话 | eval-003 | qa:eval-003@0.938 | direct | True |
| 客服工作时间 | eval-003 | qa:eval-003@0.818 | direct | True |
| 公司成立哪一年 | eval-004 | qa:eval-004@0.958 | direct | True |
| 公司成立时间 | eval-004 | qa:eval-004@0.824 | direct | True |
| 微信付款 | eval-005 | qa:eval-005@0.317 | fail_closed | True |
| 支付宝支持 | eval-005 | qa:eval-005@0.315 | fail_closed | True |
| 发票怎么开 | eval-006 | qa:eval-055@0.812 | direct | False |
| 电子发票 | eval-006 | qa:eval-058@0.816 | direct | False |
| 优惠券叠加 | eval-007 | qa:eval-007@0.446 | fail_closed | True |
| 优惠券每单限用 | eval-007 | qa:eval-007@0.320 | fail_closed | True |
| 地址填错 | eval-008 | qa:eval-008@0.436 | fail_closed | True |
| 收货地址修改 | eval-008 | qa:eval-008@0.322 | fail_closed | True |
| fahuo | eval-001 | qa:eval-001@0.291 | fail_closed | True |
| 今天天气怎么样？ | null(应拒答) | - | fail_closed | - |
| 讲个笑话吧 | null(应拒答) | - | fail_closed | - |
| 量子力学是什么？ | null(应拒答) | - | fail_closed | - |
| 你们多久能发货啊？ | eval-001 | qa:eval-001@0.812 | direct | True |
| 我下单之后要等几天才发货？ | eval-001 | qa:eval-002@0.647 | maybe_multi | True |
| 发货快不快？ | eval-001 | qa:eval-011@0.654 | maybe_multi | True |
| 请问发货周期是多长时间？ | eval-001 | qa:eval-001@0.798 | direct | True |
| 不想要了可以退吗？ | eval-002 | qa:eval-040@0.296 | fail_closed | False |
| 退货的话运费谁出？ | eval-002 | qa:eval-038@0.656 | maybe_multi | True |
| 七天无理由是真的吗？ | eval-002 | qa:eval-039@0.646 | maybe_multi | False |
| 请问退货期限是多久？ | eval-002 | qa:eval-002@0.788 | direct | True |
| 客服电话给我一下 | eval-003 | qa:eval-003@0.657 | maybe_single | True |
| 人工客服的工作时间是？ | eval-003 | qa:eval-003@0.657 | maybe_single | True |
| 有400电话吗？ | eval-003 | qa:eval-003@0.818 | direct | True |
| 怎么打你们客服？ | eval-003 | qa:eval-008@0.655 | maybe_multi | True |
| 你们公司成立多少年了？ | eval-004 | qa:eval-004@0.659 | maybe_single | True |
| 公司总部在哪？ | eval-004 | qa:eval-078@0.297 | fail_closed | True |
| 这家公司是哪年创办的？ | eval-004 | qa:eval-004@0.657 | maybe_single | True |
| 公司成立时间？ | eval-004 | qa:eval-004@0.941 | direct | True |
| 平时用微信支付行不行 | eval-005 | qa:eval-049@0.645 | maybe_multi | False |
| 支付宝付款支持吗 | eval-005 | qa:eval-005@0.158 | fail_closed | True |
| 可以用微信付款吗 | eval-005 | qa:eval-005@0.317 | fail_closed | True |
| 支持支付宝吗 | eval-005 | qa:eval-005@0.314 | fail_closed | True |
| 能开发票吗 | eval-006 | qa:eval-055@0.159 | fail_closed | False |
| 发票抬头怎么填 | eval-006 | qa:eval-057@0.658 | maybe_multi | False |
| 开票需要备注什么 | eval-006 | qa:eval-068@0.654 | maybe_multi | False |
| 怎么开电子发票 | eval-006 | qa:eval-058@0.656 | maybe_multi | False |
| 两张券能一起用吗 | eval-007 | qa:eval-036@0.151 | fail_closed | False |
| 一单可以用几张优惠券 | eval-007 | qa:eval-007@0.148 | fail_closed | True |
| 优惠券有使用限制吗 | eval-007 | qa:eval-007@0.156 | fail_closed | True |
| 优惠券能叠加吗 | eval-007 | qa:eval-007@0.465 | maybe_single | True |
| 地址写错了还能改吗 | eval-008 | qa:eval-008@0.158 | fail_closed | True |
| 怎么修改收货地址 | eval-008 | qa:eval-008@0.323 | fail_closed | True |
| 发货了才发现地址错了怎么办 | eval-008 | qa:eval-008@0.777 | direct | True |
| 改地址找谁 | eval-008 | qa:eval-008@0.158 | fail_closed | True |
| 今天适合出门吗 | null(应拒答) | - | fail_closed | - |
| 帮我写一封辞职信 | null(应拒答) | - | fail_closed | - |
| 你们支持货到付款吗 | null(应拒答) | - | fail_closed | - |
| 有纸质发票吗 | null(应拒答) | - | direct | - |
| 附近有什么好吃的 | null(应拒答) | - | fail_closed | - |
| 讲个恐怖故事 | null(应拒答) | - | fail_closed | - |
| 你们老板是谁 | null(应拒答) | - | fail_closed | - |
| 下单之后多久能出库啊 | eval-009 | qa:eval-009@0.784 | direct | True |
| 你们一般发哪家快递 | eval-010 | qa:eval-011@0.653 | maybe_multi | True |
| 运费怎么收的 | eval-012 | qa:eval-038@0.800 | direct | True |
| 新疆能发货吗 | eval-013 | qa:eval-001@0.645 | maybe_multi | True |
| 发货之后大概几天能到 | eval-015 | qa:eval-002@0.647 | maybe_multi | False |
| 可以加急发货吗 | eval-020 | qa:eval-007@0.656 | maybe_multi | False |
| 我能自己去仓库取货吗 | eval-022 | qa:eval-046@0.151 | fail_closed | True |
| 快递丢了怎么赔 | eval-025 | qa:eval-025@0.777 | direct | True |
| 收到货不喜欢可以退吗 | eval-029 | qa:eval-029@0.783 | direct | True |
| 退款多久能到账 | eval-033 | qa:eval-033@0.798 | direct | True |
| 退款是退到哪个账户 | eval-034 | qa:eval-043@0.782 | direct | True |
| 赠品需要一起寄回去吗 | eval-036 | qa:eval-036@0.303 | fail_closed | True |
| 换货要怎么申请 | eval-037 | qa:eval-037@0.967 | direct | True |
| 超过七天还能退吗 | eval-039 | qa:eval-039@0.952 | direct | True |
| 定制款能退吗 | eval-040 | qa:eval-040@0.779 | direct | True |
| 上门取件要收费吗 | eval-047 | qa:eval-047@0.660 | maybe_single | True |
| 可以用信用卡付款吗 | eval-049 | qa:eval-049@0.450 | fail_closed | True |
| 花呗分期可以吗 | eval-050 | qa:eval-050@0.943 | direct | True |
| 对公转账行不行 | eval-051 | qa:eval-051@0.160 | fail_closed | True |
| 重复扣款了怎么办 | eval-053 | qa:eval-053@0.451 | maybe_multi | True |
| 能开增值税专用发票吗 | eval-055 | qa:eval-055@0.475 | maybe_single | True |
| 发票多久能开出来 | eval-056 | qa:eval-056@0.968 | direct | True |
| 发票抬头写错了能改吗 | eval-057 | qa:eval-057@0.795 | direct | True |
| 单笔最多能付多少钱 | eval-065 | qa:eval-065@0.652 | maybe_single | True |
| 怎么才能变成会员 | eval-069 | qa:eval-069@0.931 | direct | True |
| 积分是怎么算的 | eval-071 | qa:eval-071@0.811 | direct | True |
| 积分能当钱用吗 | eval-072 | qa:eval-072@0.809 | direct | True |
| 积分会不会清零 | eval-073 | qa:eval-073@0.779 | direct | True |
| 怎么升级到金卡 | eval-075 | qa:eval-075@0.448 | fail_closed | True |
| 优惠券在哪里领 | eval-078 | qa:eval-078@0.459 | maybe_single | True |
| 新用户有什么优惠 | eval-082 | qa:eval-082@0.971 | direct | True |
| 有哪几种颜色可选 | eval-089 | qa:eval-089@0.274 | fail_closed | True |
| 功率是多少瓦 | eval-090 | qa:eval-090@0.943 | direct | True |
| 保修期多久 | eval-093 | qa:eval-001@0.797 | direct | True |
| 是正品行货吗 | eval-094 | qa:eval-094@0.450 | fail_closed | True |
| 工作的时候噪音大不大 | eval-098 | qa:eval-098@0.299 | fail_closed | True |
| 整机防水吗 | eval-099 | qa:eval-099@0.964 | direct | True |
| 能连智能音箱吗 | eval-105 | qa:eval-105@0.305 | fail_closed | True |
| 你们有实体店吗 | null(应拒答) | - | fail_closed | - |
| 你们有微信公众号吗 | null(应拒答) | - | fail_closed | - |
| 帮我翻译一下这句话 | null(应拒答) | - | maybe_single | - |

（tmp 库为一次性临时库，跑完即弃；完整机器可读摘要见 docs/eval-v3-summary.json）

