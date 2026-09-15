"""评测基线 runner：当前默认阈值下四路 + 融合 + 判定 → docs/eval-baseline.md。

用法：
    python scripts/eval_baseline.py            # 写入/替换「v2 修复后基线」节
    python scripts/eval_baseline.py --dry-run  # 只打印，不落盘
    python scripts/eval_baseline.py --section v1   # 生成「v1 节」（复现用）
    python scripts/eval_baseline.py --section v3 --before docs/eval-v2-summary.json
                                               # v3：真实规模语料后基线（需 v2 摘要做同题对照）

- 已有 docs/eval-<section>-summary.json 时**默认拒绝覆盖**（历史基线的机器可读
  记录被覆盖就追不回来了）；确要覆盖加 `--force`。

- 阈值一律默认值（TH_DIRECT=0.75/TH_MAYBE=0.45/GAP=0.15，各路门限与权重），
  不改；调整按 PRD 标定顺序（s_i→w_i→阈值）并记录（R15）。
- 种子库：sidecar/tests/eval/seed_demo.json（8 QA + 4 字段，QA 均带 category
  以还原 Markdown 导入形态、激活字段联动）；embedding 批量推理在事务外，
  同一短事务写 qa+vec（R6 示范）。
- **追加而非覆盖**：文档里的历史节是证据，不能被新一轮覆盖掉。脚本只替换
  同名节（`## v2 ...`），其余原样保留。
- 需本地模型（sidecar/models/bge-small-zh-v1.5/），缺失即 loudly 失败。
"""

import argparse
import datetime
import json
import re
import sys
import tempfile
import time
from pathlib import Path

import sqlite_vec

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sidecar" / "src"))
sys.path.insert(0, str(ROOT / "sidecar" / "tests" / "eval"))

from database.connection import connect
from database.schema import run_migrations
from knowledge.compiler import compile_store
from knowledge.field_extractor import extract, load_vocab
from knowledge.parsers.json_parser import parse_json
from knowledge.stores import create_store
from knowledge.validator import validate_field_items, validate_qa_items
from loader import load_eval_items
from models.registry import BGE_SMALL_ZH, MODEL_REGISTRY, default_model_dir
from retrieval.embedder import Embedder
from retrieval.hybrid_rank import GAP, TH_DIRECT, TH_MAYBE
from runner import evaluate_full

OUT = ROOT / "docs" / "eval-baseline.md"


def run_eval() -> dict:
    """跑一次完整评测，返回 summary（含 details 与 qa_docs）。"""
    model_dir = default_model_dir(BGE_SMALL_ZH)
    missing = [f for f in ("model.onnx", "model.onnx_data", "tokenizer.json")
               if not (model_dir / f).is_file()]
    if missing:
        raise SystemExit(f"本地模型缺失：{missing}（先跑模型下载）")

    embedder = Embedder(model_dir)
    tmp = Path(tempfile.mkdtemp(prefix="baseline-"))
    conn = connect(str(tmp / "base.db"))
    run_migrations(conn)
    store = create_store(conn, "baseline-demo")

    seed = json.loads((ROOT / "sidecar/tests/eval/seed_demo.json").read_text(encoding="utf-8"))
    parsed = parse_json(seed)
    qa_items, _ = validate_qa_items(parsed["qa_items"])
    fields_raw, _ = validate_field_items(parsed["field_items"])
    field_items, miss = extract(fields_raw, load_vocab())

    texts = [q["standard_question"] for q in qa_items]
    vecs = embedder.embed(texts)  # 事务外批量推理（R6）
    for q in qa_items:
        q.setdefault("id", q["standard_question"])
    embeddings = {
        q["id"]: sqlite_vec.serialize_float32(v.tolist())
        for q, v in zip(qa_items, vecs)
    }
    # compile_store 按 question 匹配既有行、显式 id 透传，embeddings 对齐。
    stats = compile_store(conn, store["id"], qa_items, field_items,
                          vocab_miss=miss, embeddings=embeddings)

    items = load_eval_items(ROOT / "sidecar/tests/eval/questions_100.jsonl")
    t0 = time.perf_counter()
    summary = evaluate_full(conn, store["id"], items, embedder.embed)
    summary["elapsed_s"] = time.perf_counter() - t0
    summary["stats"] = stats
    summary["tmp"] = str(tmp)
    # 语料构成随摘要一起落盘：render() 不再硬编码「8 QA + 4 字段」——
    # 扩库后那种硬编码会变成假话（v3 就是这么发现的）。
    summary["corpus"] = {
        "qa": len(qa_items),
        "fields": len(field_items),
        "entities": [k for k in seed if k != "qa_pairs"],
    }
    # 旧 59 题（v1/v2 的题目集）在新语料下的子集成绩：语料从 8 篇涨到 108 篇，
    # 整体 Top-3 的变化里混着「新题更难」与「老题被更多干扰项淹没」两种成分，
    # 只有把老题单独切出来才能把后者量出来。题目顺序即文件顺序，前 59 行即旧集。
    summary["legacy59"] = _subset(summary, 59)
    summary["gaps"] = _gaps(summary)
    conn.close()
    return summary


def _gaps(summary: dict) -> dict:
    """本轮暴露的缺口（供 v3 节逐条列证）。

    - wrong_direct：**自信地答错**（action=direct 且 top1 ≠ 期望）—— PRD 红线。
    - wrong_maybe：maybe_* 档答错（交用户判断，危险度低一档，但仍是错答案）。
    - null_open：应拒答却给了动作（含 direct）。
    """
    det = summary["details"]
    wrongs = [d for d in det if d.get("wrong")]
    return {
        "wrong_direct": [
            {"q": d["question"], "expected": d["expected_qa_id"],
             "got": (d["top1"] or {}).get("key"), "score": d["top1_score"]}
            for d in wrongs if d["action"] == "direct"
        ],
        "wrong_maybe": [
            {"q": d["question"], "expected": d["expected_qa_id"],
             "got": (d["top1"] or {}).get("key"), "score": d["top1_score"],
             "action": d["action"]}
            for d in wrongs if d["action"] != "direct"
        ],
        "null_open": [
            {"q": d["question"], "action": d["action"]}
            for d in det if not d["scored"] and d["action"] != "fail_closed"
        ],
    }


def _subset(summary: dict, upto: int) -> dict:
    """从 details 前缀切出子集成绩（口径与 evaluate_full 一致）。"""
    det = summary["details"][:upto]
    scored = [d for d in det if d["scored"]]
    nulls = [d for d in det if not d["scored"]]
    answered = [d for d in scored if d.get("answered")]
    return {
        "n": len(det),
        "scored": len(scored),
        "null_items": len(nulls),
        "top3_rate": sum(d["top3_hit"] for d in scored) / len(scored) if scored else 0.0,
        "top5_rate": sum(d["top5_hit"] for d in scored) / len(scored) if scored else 0.0,
        "answered": len(answered),
        "wrong_answers": sum(1 for d in scored if d.get("wrong")),
        "null_fail_closed_rate": (
            sum(1 for d in nulls if d.get("action") == "fail_closed") / len(nulls)
            if nulls
            else 0.0
        ),
    }


def _action_ok(action: str) -> bool:
    """动作是否「给出了答案」（非拒答）。"""
    return action in ("direct", "maybe_single", "maybe_multi")


def render(section: str, summary: dict, before: dict | None) -> str:
    date = datetime.date.today().isoformat()
    title = {
        "v1": f"## {section}（D6 前基线，{date}）",
        "v2": f"## {section}（匹配根因修复后基线，{date}）",
        "v3": f"## {section}（真实规模语料后基线，{date}）",
    }.get(section, f"## {section}（{date}）")
    L = [title, ""]
    if section == "v3":
        L += [
            "> 本轮**只动语料与题目，引擎一行没改**：阈值（TH_DIRECT/TH_MAYBE/GAP、"
            "bm25 门限、vec 门限）与权重 w 全部沿用 v2，匹配逻辑未动。",
            "",
            "### 先说清楚：v2 ↔ v3 的汇总数字**不可比**",
            "",
            "v2 是「8 篇文档里找 1 篇」，v3 是「108 篇文档里找 1 篇」——**这是两台仪器**，"
            "而且分母也变了（49 → 87 scored）。汇总列的差值没有「变好 / 变坏」的含义。"
            "要量语料规模的影响，只能拿**同一批题**在新旧语料下比：",
            "",
            "### 旧 59 题：同一批题、两种语料规模",
            "",
            "| 指标 | 旧 59 题 @ 8 文档（v2） | 旧 59 题 @ 108 文档（v3） | Δ |",
            "|---|---|---|---|",
        ]
        if before is None:
            L.append("| （缺 v2 摘要） | - | - | - |")
        else:
            leg = summary.get("legacy59", {})
            for label, a, b, fmt, dfmt in [
                ("Top-3", before["top3_rate"], leg.get("top3_rate"), "{:.3f}", "{:+.3f}"),
                ("Top-5", before["top5_rate"], leg.get("top5_rate"), "{:.3f}", "{:+.3f}"),
                ("答了（题）", before["answered"], leg.get("answered"), "{:d}", "{:+d}"),
                ("答错（题）", before["wrong_answers"], leg.get("wrong_answers"),
                 "{:d}", "{:+d}"),
                ("null 题 Fail-Closed 率", before["null_fail_closed_rate"],
                 leg.get("null_fail_closed_rate"), "{:.3f}", "{:+.3f}"),
            ]:
                if b is None:
                    L.append(f"| {label} | {fmt.format(a)} | - | - |")
                else:
                    L.append(
                        f"| {label} | {fmt.format(a)} | {fmt.format(b)}"
                        f" | {dfmt.format(b - a)} |"
                    )
        L += [
            "",
            "（口径：两列同一份 `evaluate_full`、同一组默认阈值。左列即 v2 节的汇总数字"
            "——v2 的题目集本来就是这 59 题；右列是从本轮 details 前缀切出的子集。"
            "题目顺序 = 文件顺序，故「前 59 行」就是旧集。）",
            "",
            "### 语料规模效应（本轮实测）",
            "",
            f"- 语料：qa_docs = **{summary['qa_docs']}**（v2 为 8）；题目 {summary['n']} 题"
            f"（{summary['scored']} scored + {summary['null_items']} 应拒答）。",
            "- FTS5 的 idf = `ln((N-n+0.5)/(n+0.5))`：单词命中在 N=8 时 idf=ln(5.0)≈1.61，"
            "N=108 时 idf=ln(107.5/1.5)≈4.27。**idf 塌缩在 8 文档语料上是结构性的**，"
            "本轮扩库后该问题消除，Top-3 才具备可外推性。",
            "- 108 篇语料中 **62 篇没有对应题目**，是纯干扰项（真实知识库里绝大多数条目"
            "不会被问到）。v2 的 8 篇里没有这种干扰项，这是两个基线不可比的第二个原因。",
            "",
        ]
        gaps = summary.get("gaps", {})
        L += [
            "### 已知缺口（本轮暴露；属 D6 查询理解 / 标定范围，**本轮一例未修**）",
            "",
            "v2 的「Top-3 0.980 / 答错 1 例 / null 全拒答」在 108 篇语料上**没有被复现**："
            f"{summary['scored']} scored 题里答了 {summary['answered']} 题、"
            f"**答错 {summary['wrong_answers']} 题**（答错率 "
            f"{summary['wrong_answer_rate']:.3f}），其中 **direct 档错答 "
            f"{len(gaps.get('wrong_direct', []))} 例**（自信地答错，PRD 红线）；"
            f"{summary['null_items']} 道应拒答题里 **"
            f"{len(gaps.get('null_open', []))} 道未拒答**。",
            "",
            "#### 1) direct 档错答（最严重：用户看到的是「确定答案」）",
            "",
            "| 问题 | 期望 | 实得 | 分 |",
            "|---|---|---|---|",
        ]
        for d in gaps.get("wrong_direct", []):
            L.append(f"| {d['q']} | {d['expected']} | {d['got']} | {d['score']:.3f} |")
        if not gaps.get("wrong_direct"):
            L.append("| （无） | - | - | - |")
        L += [
            "",
            "#### 2) 其余错答（maybe_* 档，交用户判断）",
            "",
            "| 问题 | 期望 | 实得 | 分 | 动作 |",
            "|---|---|---|---|---|",
        ]
        for d in gaps.get("wrong_maybe", []):
            L.append(
                f"| {d['q']} | {d['expected']} | {d['got']} | {d['score']:.3f}"
                f" | {d['action']} |"
            )
        if not gaps.get("wrong_maybe"):
            L.append("| （无） | - | - | - | - |")
        L += [
            "",
            "#### 3) 应拒答却给了动作",
            "",
            "| 问题 | 动作 |",
            "|---|---|",
        ]
        for d in gaps.get("null_open", []):
            L.append(f"| {d['q']} | {d['action']} |")
        if not gaps.get("null_open"):
            L.append("| （无） | - |")
        L += [
            "",
            "#### 机制（已用四路探针逐条定位，可复现）",
            "",
            "① **字段路的 entity 级联动在真实规模语料下失去区分度。**"
            "字段命中会把**同 entity 的全部 QA** 拉到 `s.field=1.0`（不是命中字段本身的 0.8），"
            "于是「支付与发票」的 20 篇一律拿满 +3.0/6.0。8 篇语料时这不是问题"
            "（同 entity 只有 8 篇、且各题用词不重）；20 篇同义词群时排序退化为 bm25 噪声："
            "`发票怎么开` 的 top3 挤在 **0.812 / 0.809 / 0.809（极差 0.003）**，"
            "越过 TH_DIRECT=0.75 直接进 direct 档。",
            "",
            "② **包含匹配对「通用疑问词」没有防线。**"
            "`field_vocab.json` 里 `发货周期` 的别名含 `多久发货`，而 `instr(alias, kw)` 是"
            "子串判定 —— 关键词 `多久` 因此命中了 `发货周期` 字段。后果是跨 entity 污染："
            "`保修期多久`（期望 eval-093，产品与规格）被 `发货周期`→eval-001（公司信息）"
            "拉成 **direct @0.797**。`field_lookup` 的注释已用「词表里没有 `支持`」解释过"
            "null 题为何安全，但没有覆盖「词表里的别名**含有**通用疑问词」这一情形。",
            "",
            "③ **对抗性 null 的有效期与语料规模绑定。**"
            "`有纸质发票吗` 在 8 篇语料下拒答（v2 有记录），在 108 篇的发票题群下变成 "
            "**direct** —— 词袋无法区分「问纸质发票是否存在」与「问发票怎么开」。"
            "本轮新增的两道对抗 null（`你们有实体店吗` / `你们有微信公众号吗`）都正常拒答，"
            "说明问题不在「对抗性」而在「语料里出现了同词族的答案」。",
            "",
            "**纪律**：以上三条都落在 R15 的标定范围内（查询理解 / 匹配规则 / 阈值），"
            "本轮**没有动引擎一行** —— 阈值、权重、匹配逻辑全部等于 v2。"
            "把它们留给 D6，而不是为了把本轮数字做好看而当场调参。",
            "",
        ]
    elif section == "v2":
        L += [
            "> 本轮只改**匹配**：查询关键词化（FTS 由 AND 改关键词 OR）、字段直查增加"
            "受控词表内的包含匹配。**阈值一个都没动**（R15）：TH_DIRECT/TH_MAYBE/GAP、"
            "bm25 门限、vec 门限、w 全部沿用默认值。",
            "",
            "### 与 v1 对比（同一套仪器）",
            "",
            "| 指标 | v1 | v2 | Δ |",
            "|---|---|---|---|",
        ]
        if before is None:
            L.append("| （v1 未测） | - | - | - |")
        else:
            pairs = [
                ("Top-3", "top3_rate", "{:.3f}"),
                ("Top-5", "top5_rate", "{:.3f}"),
                ("字段命中率", "field_hit_rate", "{:.3f}"),
                ("Fail-Closed 率", "fail_closed_rate", "{:.3f}"),
                ("直接返回率", "direct_rate", "{:.3f}"),
                ("null 题 Fail-Closed 率", "null_fail_closed_rate", "{:.3f}"),
            ]
            for label, key, fmt in pairs:
                a, b = before[key], summary[key]
                L.append(
                    f"| {label} | {fmt.format(a)} | {fmt.format(b)} | {b - a:+.3f} |"
                )
            # v1 的「答了/答错」由 v1 节逐题表（同一 runner 的原始输出）汇总而来，
            # 故不进对比表而写进脚注 —— 数字必须能追到出处，不能顺手默认成 0。
            v1_actions = before.get("actions", {})
            v1_answered = sum(v for k, v in v1_actions.items() if k != "fail_closed")
            L += [
                "",
                "**PRD 红线（答错）**：只有真的给出答案才可能答错，拒答是安全侧、不计入分母。"
                f"v1 答了 {v1_answered} 题（{v1_actions.get('direct', 0)} direct + "
                f"{v1_actions.get('maybe_single', 0)} maybe_single），逐题表里 top1 全部等于期望"
                f" → 答错 **0**；v2 答了 **{summary['answered']}** 题、答错 "
                f"**{summary['wrong_answers']}** 题（答错率 {summary['wrong_answer_rate']:.3f}）。",
                "",
                "### 对 v1 节「零答错」的更正",
                "",
                "v1 节写着「零答错：25 个 miss 全部是空池拒答，无一错答」。按 v1 当时的输出，"
                "这句话**字面成立**（答了 4 题、全对）——但不能读成「匹配质量好」："
                "v1 只答了 4/49，其余 45 题压根没进打分环节（AND 语义整路归零 → 空池拒答）。",
                "把召回修好之后，**同一套题目**立刻暴露出 1 例错答（见下节）。"
                "也就是说 v1 的「零错答」是**低召回的副产品**，不是安全设计的结果："
                "PRD 红线在 v1 上并没有被验证过，只是没有被触发。",
                "",
                "（历史节不改：v1 节原样保留，更正记在这里，两处数字都可回溯到各自的逐题表。）",
                "",
                "### 已知残留（1 例，根因已定位）",
                "",
                "`我下单之后要等几天才发货？`（期望 eval-001，实得 **eval-006 @0.628**，动作 maybe_multi）。",
                "",
                "关键词 `下单 / 之后 / 几天 / 发货` 在 8 篇 QA 里各投一票：`下单`→eval-006、"
                "`几天`→eval-002、`发货`→eval-001 与 eval-008。四篇的 jieba bm25 落在 "
                "−1.25 ~ −1.64，字段路对四篇一视同仁（都联动 s=1.0、不区分），"
                "融合分因此挤成 **0.628 / 0.627 / 0.623 / 0.619（极差 0.009）**；"
                "top1 落在 eval-006 只是因为它那一票最强（bm25 最负）。",
                "**这是词袋检索在 8 文档语料上的固有歧义，不是实现缺陷**："
                "极差 0.009 远够不上 GAP=0.15，系统判 `maybe_multi`（交用户判断），"
                "正是 PRD 期望的保守行为 —— 它没有「自信地答错」。",
                "v1 之所以「安全」，是因为 AND 语义让这条路整体落空 → 空池拒答；OR 化把召回换来了，"
                "顺带暴露了这个歧义。**没有用启发式去「修」它**（例如给疑问词位置加权、给短词降权），"
                "那属于标定阶段的事（R15）。",
                "",
                "### 被否决的变体（记录在案）",
                "",
                "把字符级 OR 从「仅 ≤2 关键词」扩到「所有中文查询」（simple 路恒为字符 OR）后实测：",
                "Top-3 **1.000**、direct 0.356、null 仍 10/10 —— 但同一道残留题的 top1 从"
                "`maybe_multi` 档的 eval-006@0.628 变成 **eval-002@0.773**，"
                "越过 TH_DIRECT=0.75 → **`direct`（自信地答错）**。",
                "按 PRD「宁可错杀」取向，取 Top-3 低 0.02 但错答停在「交用户判断」档的实现。"
                "（判定对 ≥0.75 是短路、不看 GAP，所以 0.773 必然进 direct 档。）",
                "（另：中文查询下 `simple_query(raw)` 是字符级 AND，恒为 `[]`，故「字符 OR ⊇ 字符 AND」，"
                "本实现与「并集」语义等价。）",
            ]
        L += [
            "",
            f"### 语料规模效应（必读）",
            "",
            f"- 本轮语料 **qa_docs = {summary['qa_docs']}**，题目 {summary['n']} 题"
            f"（{summary['scored']} scored + {summary['null_items']} 应拒答）。",
            "- FTS5 的 idf = `ln((N-n+0.5)/(n+0.5))`，N 小则 idf 塌缩、bm25 量级随之变小；"
            "**Top-3 这类召回指标在 8 文档语料上不可外推**。",
            "- 实测：本语料下真实命中的 bm25 落在 −1.2 ~ −6.0，仍远低于 −0.5 门限，"
            "即**本轮的门限没有被小语料卡住**；小语料真正影响的是 OR 化后同池文档数变多、"
            "idf 下降，导致单题绝对分整体下移（相对排序才是本轮的收益来源）。",
            "",
        ]
    else:
        L += [
            "> 阈值一律默认值，未改。调整按 PRD 标定顺序（s_i→w_i→阈值）并记录。",
            "",
        ]

    L += [
        "### 语料与模型",
        "",
        f"- 种子：sidecar/tests/eval/seed_demo.json（{summary['corpus']['qa']} QA + "
        f"{summary['corpus']['fields']} 字段；类目 {len(summary['corpus']['entities'])} 个："
        f"{'、'.join(summary['corpus']['entities'])}）",
        f"- 题目：sidecar/tests/eval/questions_100.jsonl（{summary['n']} 题："
        f"{summary['scored']} scored + {summary['null_items']} 应 Fail-Closed）",
        f"- embedding：{MODEL_REGISTRY[BGE_SMALL_ZH]['display']}（base {MODEL_REGISTRY[BGE_SMALL_ZH]['base_model']}，"
        "dim 512，L2 归一化，事务外批量）",
        f"- 入库统计：qa_inserted={summary['stats']['qa_inserted']} "
        f"fields_upserted={summary['stats']['fields_upserted']} "
        f"vec_written={summary['stats']['vec_written']}",
        "",
        "### 阈值（默认）",
        "",
        "| 常量 | 值 |",
        "|---|---|",
        f"| TH_DIRECT | {TH_DIRECT} |",
        f"| TH_MAYBE | {TH_MAYBE} |",
        f"| GAP | {GAP} |",
        "| bm25 门限 | -0.5（→s=0.5，T_fts=0.5） |",
        "| vec 门限 | d<0.6（→s=0.7） |",
        "| w | field:3, jieba:1, simple:1, vec:1（Σ=6.0） |",
        "",
        "### 数字",
        "",
        f"- qa_docs：{summary['qa_docs']}；scored：{summary['scored']}；null：{summary['null_items']}",
        f"- Top-3：{summary['top3_rate']:.3f}；Top-5：{summary['top5_rate']:.3f}",
        f"- 字段命中率：{summary['field_hit_rate']:.3f}",
        f"- 答了 {summary['answered']} 题、答错 {summary['wrong_answers']} 题"
        f"（答错率 {summary['wrong_answer_rate']:.3f}）",
        f"- 直接返回率：{summary['direct_rate']:.3f}；Fail-Closed 率：{summary['fail_closed_rate']:.3f}",
        f"- null 题 Fail-Closed 率：{summary['null_fail_closed_rate']:.3f}（{summary['null_items']} 题）",
        f"- 动作分布：{summary['actions']}",
        "- 各路平均延迟（ms）："
        + "；".join(f"{k}={v:.2f}" for k, v in summary["avg_ms"].items()),
        f"- 全量耗时：{summary['elapsed_s']:.1f}s",
        "",
        "### 逐题",
        "",
        "| 题目 | 期望 | top1 | 动作 | top3 |",
        "|---|---|---|---|---|",
    ]
    for d in summary["details"]:
        top1 = d.get("top1")
        top1s = f"{top1['type']}:{str(top1['key'])[:12]}@{top1['score']:.3f}" if top1 else "-"
        L.append(
            f"| {d['question']} | {d.get('expected_qa_id') or 'null(应拒答)'} | {top1s}"
            f" | {d.get('action', '-')} | {d.get('top3_hit', '-')} |"
        )
    L += [
        "",
        "（tmp 库为一次性临时库，跑完即弃；完整机器可读摘要见 docs/eval-"
        + section
        + "-summary.json）",
        "",
    ]
    return "\n".join(L)


def write_section(section: str, block: str) -> None:
    """把 `## <section> ...` 整节替换/追加进文档，其余内容原样保留。

    替换用 **lambda**：block 里含 Windows 路径（`C:\\Users\\...`），
    直接当替换串会被 `re` 当模板解析（`bad escape \\U`）。
    """
    text = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
    pat = re.compile(rf"^## {re.escape(section)}\b.*?(?=^## |\Z)", re.S | re.M)
    if pat.search(text):
        text = pat.sub(lambda _m: block.rstrip() + "\n\n", text)
    else:
        text = text.rstrip() + "\n\n" + block.rstrip() + "\n"
    OUT.write_text(text, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", default="v2")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="允许覆盖已存在的 docs/eval-<section>-summary.json")
    ap.add_argument("--before", help="v1 的 summary JSON（用于生成对比表）")
    args = ap.parse_args()

    # 先挡覆盖，再跑（run_eval 要下模型、要几秒）：历史基线的机器可读摘要
    # 一旦被覆盖就再也追不回来 —— 例如 v2 的数字被本轮 v3 覆盖掉，
    # v3 节的「同题对照」就失去左列。默认拒绝，要覆盖请显式 --force。
    dump = ROOT / "docs" / f"eval-{args.section}-summary.json"
    if dump.exists() and not args.force:
        raise SystemExit(
            f"拒绝覆盖 {dump.name}：它可能是某个历史基线的唯一机器可读记录。\n"
            f"确要覆盖请加 --force（覆盖前建议先 cp 到 .workbuddy-ai/backup/）。"
        )

    summary = run_eval()
    before = json.loads(Path(args.before).read_text(encoding="utf-8")) if args.before else None
    block = render(args.section, summary, before)
    print(block[:1600])
    # 红线优先打印：答错的题必须当场可见，不能只留一个汇总数字。
    wrongs = [d for d in summary["details"] if d.get("wrong")]
    if wrongs:
        print("\n!! PRD 红线：以下题目给出了错误答案")
        for d in wrongs:
            print(
                f"   {d['question']!r} 期望 {d['expected_qa_id']} "
                f"实得 {d['top1']['key'] if d.get('top1') else None} "
                f"@{d['top1_score']:.3f} 动作 {d['action']}"
            )
    else:
        print(f"\nPRD 红线：答了 {summary['answered']} 题，答错 0 题")
    # 数字先落盘：即使 --dry-run 也要留下可复核的摘要（对比表依赖它）。
    dump.write_text(
        json.dumps({k: v for k, v in summary.items() if k != "details"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nWROTE {dump}")
    if args.dry_run:
        print("[DRY-RUN] 未写入 docs/eval-baseline.md")
        return
    write_section(args.section, block)
    print(f"WROTE {OUT}（节 {args.section}）")


if __name__ == "__main__":
    main()
