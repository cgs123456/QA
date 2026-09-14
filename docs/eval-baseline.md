# 评测基线（四路 + 融合 + 判定，2026-09-14）

> D6 前基线：阈值一律默认值，未改。调整按 PRD 标定顺序（s_i→w_i→阈值）并记录。

## 语料与模型

- 种子：tests/eval/seed_demo.json（8 QA + 4 字段；QA 均带 category=公司信息，还原 Markdown 形态）
- 题目：tests/eval/questions_100.jsonl（20 题：17 scored + 3 应 Fail-Closed），冻结前为示例
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

- Top-3：1.000；Top-5：1.000（17 scored）
- 直接返回率：0.150；Fail-Closed 率：0.800
- null 题 Fail-Closed 率：1.000（3 题）
- 动作分布：{'maybe_single': 1, 'fail_closed': 16, 'direct': 3}
- 各路平均延迟（ms）：field=0.12；fts=29.89；vec=2.77；embed=2.43；fuse=0.03

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

（tmp 库已弃；全量耗时见运行输出）

## 基线解读（只记录现象与机制，不改阈值）

1. **Top-3=1.0 但 Fail-Closed=0.80**：检索全找得到，判定几乎全拒答。
   机制有二：
   - 字段联动只在**精确相等**时触发（“发货周期多久”≠“发货周期”，无联动），
     而自然问法极少逐字等于 field_name——联动门槛过高。
   - 纯模糊上限恒 0.5（(1+1+1)/6），三路稍弱即跌破 0.45；
     如“发货周期多久” top1=0.456（模糊三路和 2.74，相当强）仍被拒。
   只有“退货期限/客服电话/公司成立时间”三个逐字命中字段名的题走到 direct。
2. **null 题 Fail-Closed 率 1.0**：3/3 正确拒答，无编造——Fail-Closed 方向正确，
   松紧留待标定。
3. **延迟**：field 0.12ms / fts 29.89ms（含首查词典懒加载摊薄）/ vec 2.77ms /
   embed 2.43ms（逐条调用；生产应批量）/ fuse 0.03ms——均远低于预算。
4. **小语料 idf 塌缩仍在**： scored 题全 top3 命中，但 bm25 量级受语料规模影响，
   100 题真实语料上需重测（标定顺序 s_i→w_i→阈值）。
5. **未动项**：所有阈值/权重/门限均为 PRD 初值；任何调整必须记录并重跑本基线。
