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
