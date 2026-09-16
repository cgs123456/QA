# P6 评测集冻结 + 全链标定（R15 顺序执行）

标定日期：2026-09-15
评测集：`sidecar/tests/eval/questions_100.jsonl`（100 题 = 87 scored + 13 null）
语料：`sidecar/tests/eval/seed_demo.json`（**qa_docs=108**，29 字段 / 6 类目）
复现：`python scripts/eval_calibrate.py --source --dump`

> 报召回指标必须同时报 qa_docs。108 篇与 8 篇的 Top-3 不可比
> （FTS5 idf = ln((N-n+0.5)/(n+0.5))，N=8 时塌缩到 ≈1.61，N=108 时 ≈4.27）。

---

## 1. 结论：定稿常量

| 常量 | 初值 | 定稿 | 位置 |
|---|---|---|---|
| `W_FIELD` | 3.0 | **1.5** | `retrieval/hybrid_rank.py` |
| `W_JIEBA` | 1.0 | **3.0** | 同上 |
| `W_SIMPLE` | 1.0 | **1.0** | 同上 |
| `W_VEC` | 1.0 | **4.0** | 同上 |
| `W_SUM`（Σw） | 6.0 | **9.5** | 同上（= 四路之和，改 w 必须同步） |
| `TH_DIRECT` | 0.75 | **0.65** | 同上 |
| `TH_MAYBE` | 0.45 | **0.60** | 同上 |
| `GAP` | 0.15 | **0.15**（未动） | 同上 |
| `LINK_MODE` | `entity_flat` | **`entity_scaled`** | 同上 |
| `DIST_CUTOFF` | 0.6 | **0.8** | `retrieval/vector_search.py` |
| `CONTAINMENT_MIN_LEN` | 1（无此常量） | **2** | `retrieval/field_lookup.py` |
| `CONTAINMENT_IN_ALIAS` | True（无此常量） | **False** | `retrieval/field_lookup.py` |
| `T_FTS` | 0.5 | **0.5**（未动） | `retrieval/fts5_search.py` |
| `BM25_CUTOFF` | -0.5 | **-0.5**（未动） | `retrieval/fts5_search.py` |

终值指标（`--source`，源码默认常量，非覆盖值）：

| 指标 | 值 | 目标 | 判定 |
|---|---|---|---|
| Top-3 | **0.9080** | ≥ 0.85 | ✅ |
| null 拒答率 | **1.000**（13/13） | 100% | ✅ |
| direct 档答对 | **51** | 合理提升 | ✅（初值 32） |
| direct 档答错 | **5** | 不越 R15 红线 | ✅（与初值持平） |
| 答错总数 | **6** | 下降 | ✅（初值 18） |
| 答错率 | **0.102** | — | ✅（初值 0.305） |
| Fail-Closed 率（非 null 题） | **0.3218** | 不劣化 | ✅（初值 0.3218，持平） |

动作分布：`direct 56 / maybe_multi 2 / maybe_single 1 / fail_closed 41`。

---

## 2. 评测集冻结

- 文件：`sidecar/tests/eval/questions_100.jsonl`，100 行 / 87 scored / **13 null（13%，硬约束 10–15）**。
- 纪律：**只追加，不改已有行**。
- loader（`sidecar/tests/eval/loader.py`）三条硬校验，任一不满足即 `ValueError`（带行号）：
  1. **schema** — `question` 非空字符串 / `expected_qa_id` 为 string 或 null / `tags` 为 string 数组；
  2. **去重** — `question`（strip 后）不得重复。重复题会让同一道题在指标里被计权两次，
     Top-3 与拒答率随「某题抄了几遍」漂移，冻结语义失效（`test_frozen_set_has_no_duplicate_questions`）；
  3. **引用** — `expected_qa_id` 必须能在语料里解析（`test_every_expected_id_resolves_to_the_corpus`，
     建库后校验，loader 不持有语料）。

> ⚠️ 已知盲区：本集 **0 道纯字段直查题**（100 题里没有任何一题的 top1 是 field 候选）。
> 第 4 节的两个副作用正是它没覆盖到的。补题时至少覆盖 21 个无 QA 孪生的字段
> （保修期 / 防水等级 / 包邮门槛…）各一题，并配一道「精确命中字段名但意图不同」的对抗题。

---

## 3. 顺序标定（R15）：每步重跑的前后对比

`scripts/eval_calibrate.py --config '<JSON>'`，`BASELINE` 保留初值以便复现 before。
每步**单变量**（其余保持上一步）。

| 步 | 改动 | Top-3 | null 拒答 | direct 对/错 | 答了 | 答错 | 答错率 | FC(非null) |
|---|---|---|---|---|---|---|---|---|
| s0 | 初值基线 | 0.8621 | 0.846 | 32 / 5 | 59 | 18 | 0.305 | 0.3218 |
| s1 | 匹配修复：`kw≥2` + 关别名包含 + `entity_scaled` | 0.8621 | 0.923 | 12 / 0 | 57 | 16 | 0.281 | 0.3448 |
| s2 | 第 1 步：向量门限 `0.6 → 0.8` | 0.8736 | 0.923 | 16 / 1 | 57 | 16 | 0.281 | 0.3448 |
| s3 | 第 2 步：权重 `(3,1,1,1) → (1.5,3,1,4)` | 0.9080 | 0.846 | 24 / 1 | 69 | 9 | 0.130 | 0.2069 |
| s4 | 第 3 步：阈值 `0.75/0.45 → 0.65/0.60` | **0.9080** | **1.000** | 51 / 5 | 59 | **6** | 0.102 | 0.3218 |

### s0 → s1：匹配质量修复（P1 型，非阈值豁免）

三条机制性修复，都是「匹配质量」而非「放低门槛」：

1. **`CONTAINMENT_MIN_LEN = 2`** — 单字关键词不作包含匹配。
   实例：`帮我翻译一下这句话` 的单字 `话` ⊂ 字段名 `客服电话` → 误命中 → 被抬到 0.651 进 direct。
2. **`CONTAINMENT_IN_ALIAS = False`** — `kw ⊂ alias` 不再算包含命中（**方向反了**）。
   包含匹配的语义是「用户说得比**规范字段名**短」（`电话` ⊂ `客服电话`）；
   而别名是整句（`多久发货`），用短词 `多久` 去命中长别名是反向的。
   实例：`保修期多久` 的 `多久` ⊂ 别名 `多久发货` → 跨 entity 污染 → eval-001 被判 direct。
3. **`LINK_MODE: entity_flat → entity_scaled`** — 联动强度从「同 entity 一律 1.0」改为
   「= 字段命中自身的 s」。初值在 8 篇语料下无害，108 篇下同 entity 有 20 篇同义题群，
   平权把 entity 内排序**彻底抹平** → 退化为 bm25 噪声。
   实例：`发票怎么开` 的 top3 挤在 0.812/0.809/0.809（极差 0.003）越过 TH_DIRECT → direct 错答。

s1 结果：null 拒答 0.846→0.923，direct 档答错 5→**0**，答错 18→16。
代价是 direct 档答对 32→12（联动不再白送满分）—— 由 s3/s4 补回。

### s1 → s2：第 1 步，定 T_fts / 门限

- `T_FTS`：在 0.3–0.8 上扫描，**只动「答了」不动红线**，保留 0.5。
- `BM25_CUTOFF`：−0.2 ~ 0 全域**完全惰性**（指标一字不变），保留 −0.5。
- `DIST_CUTOFF`：0.6→0.8 是唯一有效项。Top-3 0.8621→0.8736、direct 对 12→16，
  代价 direct 错 0→1。**继续放宽到 1.0 会让对抗题的语义近邻进来**（答错升到 3），故停在 0.8。

### s2 → s3：第 2 步，定 w_i

语义路 `W_VEC 1.0→4.0` 是唯一**同时**提升 Top-3 与降低答错的项（它才是同义改写的判别信号）；
字段路 `3.0→1.5`（平权联动被 `entity_scaled` 取代后，它不再是唯一能推高分的路）。
Top-3 0.8736→**0.9080**，答错 16→9。

> ⚠️ `W_SUM` 是模块级常量，**改 w 必须同步重算**，否则分母不动、分值域被压缩。
> 已在 harness 的 `_apply()` 里统一处理。

### s3 → s4：第 3 步，定阈值

`TH_MAYBE 0.45→0.60` 是「null 拒答 100%」的**唯一来源**：它在匹配修复后把对抗题
`有纸质发票吗`（字段「发票类型」包含命中 → entity 联动抬到 0.801）压回 fail_closed。
`TH_DIRECT 0.75→0.65` 用于把 direct 档召回从 24 提到 51，且 direct 错不增（1→5 是因为
更多题进入 direct 档，其中 5 道错，与初值的 5 道持平）。
`GAP` 在 0.10–0.25 上**完全惰性**，保留 0.15。

稳健性：候选 A 在 `W_VEC 3.5~4.5`、`TH_DIRECT 0.65~0.70` 全域 null 拒答恒为 1.000
（不是刀尖最优）；候选 B（`w=(2,2,1,3)` / `0.60/0.55`）在 `W_VEC=2.5` 时掉到 0.923，故**选 A**。

---

## 4. 落盘前核查：两个评测集覆盖不到的副作用

s4 达标后把常量写回源码，逐条核对其副作用时发现两条**评测集完全测不到**的回归。
根因是同一个：**固定分母 Σw + 缺席路记 0 分**。

### 副作用 1：向量路一挂 → 全线 Fail-Closed

`W_VEC=4.0` 缺席时，剩余三路天花板 = (1.5+3+1)/9.5 = **0.579 < TH_MAYBE 0.60** → 100% 拒答。
直接违反已规格化的「降级不中断」（`test_vec_failure_degrades_not_breaks`）。

### 副作用 2：字段直查路径失效

纯 field 候选 = 1.5/9.5 = **0.158** → Fail-Closed。初值下是 3/6 = 0.5 ≥ 0.45，能走 maybe 答出来。
语料 29 个字段里 **21 个没有 QA 孪生**（保修期 / 防水等级 / 包邮门槛 / 单笔限额 …），
即字段直查的主体全部失效。

### 修复

**（a）分母只剔除「明确不可用」的路**（`hybrid_rank._active_weight` + `fuse(unavailable=…)`）。

- 路**抛异常 / 无索引** → 权重退出分母（没有证据 ≠ 负面证据）。
- 路**召回到 0 条** → 留在分母（那是负证据）。
- router 侧：embed 抛错记 warning 并标 `vec` 不可用；召回为空时再用
  `vector_search.has_vectors()` 判一次是「库里没向量」还是「没找到」。

**（b）精确字段直查走判定层硬规则**（`hybrid_rank.decide`）：
top1 为 field 候选且 `s.field ≥ 1.0`（精确命中，不是包含匹配的 0.8）→ direct。
安全性：13 道 null 对抗题**无一**精确命中字段名（唯一有字段命中的「有纸质发票吗」是
包含匹配 s=0.80），故不削弱拒答。

四路全活时（a）不改变任何分值（分母恒 = W_SUM），**s0–s4 全部数字不受影响**，已重跑验证。

### 踩过的坑（留档）

第一版把「召回为空」也当缺席剔除，结果 null 拒答率从 **1.000 崩到 0.154**
（11/13 道对抗题被抬进 direct）—— 对抗题正是因为**各路都找不到东西**才该被拒。
「路挂了」与「路说没找到」是两件事，只有前者该免计分。
两条反向断言已钉进 `test_unavailable_route_drops_from_denominator`。

**教训**：只看「四路全活」的指标做标定，会选出**鲁棒性更差**的常量。
后续标定须加两条硬约束：① 任一整路缺席时天花板仍 ≥ TH_MAYBE；② 纯字段直查仍可达 maybe 以上。

---

## 5. 红线与未闭合项

- **未放宽任何阈值来凑指标**：s1 是匹配质量修复，s2/s3/s4 是 R15 规定的顺序标定。
  若 100 题跑不满 85%，允许的修复仍只有匹配质量类 —— 本轮达标，未触发。
- **`CONTAINMENT_S`（0.8）、`GAP`（0.15）、`BM25_CUTOFF`（−0.5）在本轮扫描中完全惰性**，
  保留原值；这不代表它们无用，只代表 108 篇语料下分档对它们不敏感。
- **eval 集盲区**（第 2 节）：0 道纯字段直查题。补题后需重跑本文件全部五步。
- **常量的有效期**：本标定绑定 `questions_100.jsonl` + `seed_demo.json(108)`。
  语料或题集变更 → 重跑 `scripts/eval_baseline.py --section v4` 并更新本文件。

---

## 6. 复现

```bash
export NO_PROXY="127.0.0.1,localhost"   # 本机有系统级 http_proxy，会拦 127.0.0.1
PY="C:/Users/Administrator/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe"

# 终值（源码常量）
$PY scripts/eval_calibrate.py --source --dump --out .workbuddy-ai/backup/calib-final.json

# 五步对比（BASELINE 之上叠加，JSON 必须小写 false）
$PY scripts/eval_calibrate.py --config '{}'
$PY scripts/eval_calibrate.py --config '{"CONTAINMENT_MIN_LEN":2,"CONTAINMENT_IN_ALIAS":false,"LINK_MODE":"entity_scaled"}'
$PY scripts/eval_calibrate.py --config '{...,"DIST_CUTOFF":0.8}'
$PY scripts/eval_calibrate.py --config '{...,"W_FIELD":1.5,"W_JIEBA":3.0,"W_VEC":4.0}'
$PY scripts/eval_calibrate.py --config '{...,"TH_DIRECT":0.65,"TH_MAYBE":0.60}'

# 单题证据（看分从哪来）
$PY scripts/eval_calibrate.py --probe '发票怎么开'
```

单测：`cd sidecar && $PY -m pytest tests/ -q` → 396 passed, 1 skipped。
新增/更新的用例：`test_hybrid_rank.py`（常量算术 + 精确字段直查 + 分母剔除）、
`tests/eval/test_eval.py`（题目去重）、路由类用例改用 `conftest.band_s` 按常量反解档位。
