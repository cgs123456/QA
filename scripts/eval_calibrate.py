"""标定 harness：按 R15 顺序（s_i → w_i → 阈值）单变量扫参 + 逐题明细导出。

与 `eval_baseline.py` 的分工：
- `eval_baseline.py` = 用**默认常量**跑一次、写进 `docs/eval-baseline.md`（出基线）。
- 本脚本 = 用**给定常量组合**跑多次、只输出对比表（做标定）。

**本脚本不改任何源文件**：所有常量通过模块全局赋值注入（`fuse` / `decide` /
`normalize_score` / `_containment` / `vector_search` 都在**调用时**读模块全局，
所以运行期改全局是生效的，且进程退出即消失）。

用法：
    python scripts/eval_calibrate.py --dump                 # 默认常量 + 逐题明细
    python scripts/eval_calibrate.py --sweep t_fts          # 第 1 步：T_fts / bm25 门限
    python scripts/eval_calibrate.py --sweep vec            # 第 1 步附：vec 距离门限
    python scripts/eval_calibrate.py --sweep w              # 第 2 步：w_i
    python scripts/eval_calibrate.py --sweep th             # 第 3 步：TH_DIRECT/TH_MAYBE/GAP
    python scripts/eval_calibrate.py --sweep contain        # 附：CONTAINMENT_S
    python scripts/eval_calibrate.py --config '{"T_FTS":0.3,"W_FIELD":2.0}'

注意：`hybrid_rank.W_SUM` 是模块级常量（=四路之和），改 w 时必须同步重算，
否则分母不动、分值域被压缩 —— 这是最容易踩的坑，已在 `_apply` 里统一处理。
"""

import argparse
import json
import sys
import tempfile
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
from models.registry import BGE_SMALL_ZH, default_model_dir
from retrieval import field_lookup as FL
from retrieval import fts5_search as FT
from retrieval import hybrid_rank as HR
from retrieval import vector_search as VS
from retrieval.embedder import Embedder
from runner import evaluate_full

# 常量 → 所在模块的映射。加新常量只改这张表。
_REGISTRY = {
    "T_FTS": FT,
    "BM25_CUTOFF": FT,
    "TOP_K_FTS": FT,
    "CONTAINMENT_S": FL,
    "CONTAINMENT_MIN_LEN": FL,
    "CONTAINMENT_IN_ALIAS": FL,
    "DIST_CUTOFF": VS,
    "VEC_TOP_K": VS,
    "W_FIELD": HR,
    "W_JIEBA": HR,
    "W_SIMPLE": HR,
    "W_VEC": HR,
    "TH_DIRECT": HR,
    "TH_MAYBE": HR,
    "GAP": HR,
    "LINK_MODE": HR,
}

# 名称 → 模块属性的实际名（避免 TOP_K 在两个模块里撞名）
_ATTR = {
    "TOP_K_FTS": "TOP_K",
    "VEC_TOP_K": "TOP_K",
}

BASELINE = {
    "T_FTS": 0.5,
    "BM25_CUTOFF": -0.5,
    "CONTAINMENT_S": 0.8,
    # 下面三个是**冻结基线（v3）当时的取值**，不是当前源码默认值 ——
    # 保留它们是为了让「匹配修复前」可复现（否则 before 列会跟着源码漂移）。
    "CONTAINMENT_MIN_LEN": 1,
    "CONTAINMENT_IN_ALIAS": True,
    "LINK_MODE": "entity_flat",
    "DIST_CUTOFF": 0.6,
    "W_FIELD": 3.0,
    "W_JIEBA": 1.0,
    "W_SIMPLE": 1.0,
    "W_VEC": 1.0,
    "TH_DIRECT": 0.75,
    "TH_MAYBE": 0.45,
    "GAP": 0.15,
}


def _apply(cfg: dict) -> dict:
    """把 cfg 注入各模块全局，返回实际生效的全量常量（含 W_SUM 重算）。"""
    for k, v in cfg.items():
        if k not in _REGISTRY:
            raise SystemExit(f"未知常量 {k}（可用的见 _REGISTRY）")
        setattr(_REGISTRY[k], _ATTR.get(k, k), v)
    # W_SUM 必须跟着 w 走：它是模块级常量，不会自动重算。
    HR.W_SUM = HR.W_FIELD + HR.W_JIEBA + HR.W_SIMPLE + HR.W_VEC
    return current()


def current() -> dict:
    out = {k: getattr(_REGISTRY[k], _ATTR.get(k, k)) for k in BASELINE}
    out["W_SUM"] = HR.W_SUM
    return out


class Harness:
    """建库一次 + 查询向量缓存，之后可反复换常量重跑。"""

    def __init__(self):
        self.base = dict(BASELINE)
        model_dir = default_model_dir(BGE_SMALL_ZH)
        missing = [f for f in ("model.onnx", "model.onnx_data", "tokenizer.json")
                   if not (model_dir / f).is_file()]
        if missing:
            raise SystemExit(f"本地模型缺失：{missing}")
        self.embedder = Embedder(model_dir)
        self.tmp = Path(tempfile.mkdtemp(prefix="calib-"))
        self.conn = connect(str(self.tmp / "calib.db"))
        run_migrations(self.conn)
        store = create_store(self.conn, "calib-demo")

        seed = json.loads(
            (ROOT / "sidecar/tests/eval/seed_demo.json").read_text(encoding="utf-8")
        )
        parsed = parse_json(seed)
        qa_items, _ = validate_qa_items(parsed["qa_items"])
        fields_raw, _ = validate_field_items(parsed["field_items"])
        field_items, miss = extract(fields_raw, load_vocab())
        texts = [q["standard_question"] for q in qa_items]
        vecs = self.embedder.embed(texts)
        for q in qa_items:
            q.setdefault("id", q["standard_question"])
        embeddings = {
            q["id"]: sqlite_vec.serialize_float32(v.tolist())
            for q, v in zip(qa_items, vecs)
        }
        self.stats = compile_store(
            self.conn, store["id"], qa_items, field_items,
            vocab_miss=miss, embeddings=embeddings,
        )
        self.store_id = store["id"]
        self.items = load_eval_items(ROOT / "sidecar/tests/eval/questions_100.jsonl")
        self._cache: dict = {}

    def embed_fn(self, texts):
        out = []
        for t in texts:
            if t not in self._cache:
                self._cache[t] = self.embedder.embed([t])[0]
            out.append(self._cache[t])
        return out

    def run(self, cfg: dict) -> dict:
        # `self.base is None` = 用源码默认值跑（校验"落盘后的真实行为"，
        # 而不是某组覆盖值）。此时 cfg 仍可再叠加。
        eff = dict(cfg) if self.base is None else {**self.base, **cfg}
        applied = _apply(eff) if eff else current()
        s = evaluate_full(self.conn, self.store_id, self.items, self.embed_fn)
        s["config"] = applied
        return s

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def metrics(s: dict) -> dict:
    """标定关心的核心指标（红线优先）。

    `direct_ok` = direct 档**答对**的题数 —— 任务目标「direct 档召回合理提升」
    的真正度量：`direct_rate` 会把「自信地答错」也算进去，越线时它反而更好看。
    """
    det = s.get("details", [])
    exp_ok = lambda d: (d.get("top1") or {}).get("key") == d.get("expected_qa_id")
    direct = [d for d in det if d.get("action") == "direct"]
    return {
        "top3": s["top3_rate"],
        "top5": s["top5_rate"],
        "null_fc": s["null_fail_closed_rate"],
        "direct": s["direct_rate"],
        "direct_ok": sum(1 for d in direct if exp_ok(d)),
        "direct_wrong": sum(1 for d in direct if not exp_ok(d)),
        "fc": s["fail_closed_rate"],
        "answered": s["answered"],
        "wrong": s["wrong_answers"],
        "wrong_rate": s["wrong_answer_rate"],
    }


_COLS = [
    ("top3", "Top-3", "{:.4f}"),
    ("null_fc", "nullFC", "{:.3f}"),
    ("direct_ok", "direct对", "{:d}"),
    ("direct_wrong", "direct错", "{:d}"),
    ("answered", "答了", "{:d}"),
    ("wrong", "答错", "{:d}"),
    ("wrong_rate", "答错率", "{:.3f}"),
]


def table(rows: list) -> str:
    head = " | ".join(f"{label:>8}" for _, label, _ in _COLS)
    lines = [f"{'配置':<34} | {head}", "-" * (34 + 3 + len(head))]
    for name, s in rows:
        vals = " | ".join(f"{fmt.format(metrics(s)[k]):>8}" for k, _, fmt in _COLS)
        lines.append(f"{name:<34} | {vals}")
    return "\n".join(lines)


def dump_details(s: dict, path: Path, stats: dict | None = None) -> None:
    det = []
    for d in s["details"]:
        det.append({
            "question": d["question"],
            "expected": d.get("expected_qa_id"),
            "scored": d["scored"],
            "action": d.get("action"),
            "top1_score": d.get("top1_score"),
            "top1": (d.get("top1") or {}).get("key"),
            "top1_type": (d.get("top1") or {}).get("type"),
            "top3_hit": d.get("top3_hit"),
            "top5_hit": d.get("top5_hit"),
            "answered": d.get("answered"),
            "wrong": d.get("wrong"),
        })
    payload = {
        "config": s["config"],
        "metrics": metrics(s),
        "actions": s["actions"],
        "qa_docs": s["qa_docs"],
        "stats": stats or {},
        "details": det,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"WROTE {path}")


def probe(h: "Harness", q: str, top: int = 6) -> None:
    """打印单题的四路原始证据 + 融合 top-k（逐路 s 明细）。

    标定必须看得见「分从哪来」，否则只能靠猜。
    """
    from retrieval.field_lookup import field_lookup
    from retrieval.fts5_search import fts5_search
    from retrieval.hybrid_rank import fuse
    from retrieval.query_prep import prepare
    from retrieval.vector_search import vector_search

    conn, sid = h.conn, h.store_id
    prep = prepare(conn, q)
    print(f"\n{'='*78}\n题目: {q!r}")
    print(f"关键词: {getattr(prep, 'keywords', None)}")
    print(f"keyword_expr: {getattr(prep, 'keyword_expr', None)}")
    print(f"char_expr: {getattr(prep, 'char_expr', None)}")

    fld = field_lookup(conn, sid, q, prepared=prep)
    print(f"\n-- 字段路（{len(fld)}）--")
    for x in fld:
        print(f"   s={x['s']:.2f} entity={x['entity']!r} name={x['field_name']!r} "
              f"value={str(x['field_value'])[:34]!r}")

    fts = fts5_search(conn, sid, q, prepared=prep)
    for route in ("jieba", "simple"):
        rows = [x for x in fts if x["route"] == route]
        print(f"\n-- FTS {route}（{len(rows)}）--")
        for x in rows:
            print(f"   s={x['s']:.3f} bm25={x['bm25']:+.3f} {x['qa_id']} {x['standard_question'][:30]!r}")

    vec = vector_search(conn, sid, h.embed_fn([q])[0])
    print(f"\n-- vec（{len(vec)}）--")
    for x in vec:
        print(f"   s={x['s']:.3f} d={x['distance']:.3f} {x['qa_id']} {x['standard_question'][:30]!r}")

    fused = fuse(fld, [x for x in fts if x["route"] == "jieba"],
                 [x for x in fts if x["route"] == "simple"], vec)
    print(f"\n-- 融合 top{top} --")
    for c in fused[:top]:
        s = c["s"]
        print(f"   {c['score']:.4f}  {c['type']:5} {c['key']:<10} "
              f"s(f={s['field']:.2f} j={s['jieba']:.2f} si={s['simple']:.2f} v={s['vec']:.2f})"
              + (f"  {c['payload'].get('standard_question','')[:26]!r}"
                 if c["type"] == "qa" else
                 f"  {c['payload'].get('field_name','')!r}"))


def sweeps() -> dict:
    """R15 三步的候选网格。每步**单变量**（其余保持 BASELINE）。"""
    return {
        # 第 1 步：s_i 的归一化标尺（T_fts 决定 bm25→s 的曲率；门限决定截断点）
        "t_fts": [("T_fts=0.2", {"T_FTS": 0.2}), ("T_fts=0.5(默认)", {}),
                  ("T_fts=1.0", {"T_FTS": 1.0}), ("T_fts=2.0", {"T_FTS": 2.0}),
                  ("T_fts=4.0", {"T_FTS": 4.0})],
        "bm25": [("门限=-0.2", {"BM25_CUTOFF": -0.2}), ("门限=-0.5(默认)", {}),
                 ("门限=-1.0", {"BM25_CUTOFF": -1.0}),
                 ("门限=-2.0", {"BM25_CUTOFF": -2.0}),
                 ("门限=0(不过滤)", {"BM25_CUTOFF": 0.0})],
        "vec": [("d<0.4", {"DIST_CUTOFF": 0.4}), ("d<0.5", {"DIST_CUTOFF": 0.5}),
                ("d<0.6(默认)", {}), ("d<0.8", {"DIST_CUTOFF": 0.8}),
                ("d<1.0", {"DIST_CUTOFF": 1.0})],
        # 第 2 步：w_i（单变量扫，先定 field，再定 vec）
        "w": [("w=(3,1,1,1)默认", {}),
              ("w_field=1", {"W_FIELD": 1.0}),
              ("w_field=2", {"W_FIELD": 2.0}),
              ("w_field=4", {"W_FIELD": 4.0}),
              ("w_field=6", {"W_FIELD": 6.0}),
              ("w_vec=2", {"W_VEC": 2.0}),
              ("w_vec=3", {"W_VEC": 3.0}),
              ("w_vec=4", {"W_VEC": 4.0}),
              ("w_jieba=2", {"W_JIEBA": 2.0}),
              ("w_jieba=3", {"W_JIEBA": 3.0}),
              ("w_simple=0", {"W_SIMPLE": 0.0}),
              ("w_simple=2", {"W_SIMPLE": 2.0})],
        # 第 3 步：判定阈值
        "th": [("0.75/0.45/0.15 默认", {}),
               ("0.70/0.45/0.15", {"TH_DIRECT": 0.70}),
               ("0.65/0.45/0.15", {"TH_DIRECT": 0.65}),
               ("0.60/0.45/0.15", {"TH_DIRECT": 0.60}),
               ("0.55/0.45/0.15", {"TH_DIRECT": 0.55}),
               ("0.75/0.50/0.15", {"TH_MAYBE": 0.50}),
               ("0.75/0.55/0.15", {"TH_MAYBE": 0.55}),
               ("0.75/0.45/0.10", {"GAP": 0.10}),
               ("0.75/0.45/0.05", {"GAP": 0.05}),
               ("0.75/0.45/0.20", {"GAP": 0.20})],
        "contain": [("S=1.0", {"CONTAINMENT_S": 1.0}),
                    ("S=0.8 默认", {}),
                    ("S=0.6", {"CONTAINMENT_S": 0.6}),
                    ("S=0.5", {"CONTAINMENT_S": 0.5})],
        # 匹配质量修复的消融（机制①/②）。每行相对上一行只加一项，便于归因。
        "match": [
            ("A 初版（=冻结基线）", {}),
            ("B 只加 kw≥2", {"CONTAINMENT_MIN_LEN": 2}),
            ("C 只关别名包含", {"CONTAINMENT_IN_ALIAS": False}),
            ("D B+C", {"CONTAINMENT_MIN_LEN": 2, "CONTAINMENT_IN_ALIAS": False}),
            ("E D+联动=字段s", {"CONTAINMENT_MIN_LEN": 2, "CONTAINMENT_IN_ALIAS": False,
                              "LINK_MODE": "entity_scaled"}),
            ("F D+联动×文本证据", {"CONTAINMENT_MIN_LEN": 2, "CONTAINMENT_IN_ALIAS": False,
                                "LINK_MODE": "entity_text"}),
            ("G D+仅精确联动", {"CONTAINMENT_MIN_LEN": 2, "CONTAINMENT_IN_ALIAS": False,
                              "LINK_MODE": "exact_only"}),
            ("H D+不联动", {"CONTAINMENT_MIN_LEN": 2, "CONTAINMENT_IN_ALIAS": False,
                           "LINK_MODE": "off"}),
            ("I B+联动×文本证据", {"CONTAINMENT_MIN_LEN": 2, "LINK_MODE": "entity_text"}),
            ("J 初版+联动×文本证据", {"LINK_MODE": "entity_text"}),
        ],
    }


def grid_search(h: "Harness", space: dict, want_null_fc: float = 1.0,
                want_top3: float = 0.85, min_answered: int = 0,
                top: int = 25) -> list:
    """网格搜索，返回满足硬约束的候选（按 答错↑ / 答了↓ / direct↑ 排序）。

    硬约束（任务目标）：null 拒答 100%、Top-3 ≥ 85%。
    软目标：答错最少、答得最多（direct 档正确数多优先）。
    """
    import itertools

    keys = list(space)
    combos = list(itertools.product(*(space[k] for k in keys)))
    print(f"[grid] 组合数 {len(combos)}")
    hits = []
    for i, vals in enumerate(combos, 1):
        cfg = dict(zip(keys, vals))
        s = h.run(cfg)
        m = metrics(s)
        if (m["null_fc"] >= want_null_fc and m["top3"] >= want_top3
                and m["answered"] >= min_answered):
            hits.append((cfg, m))
        if i % 50 == 0:
            print(f"  …{i}/{len(combos)}  已命中 {len(hits)}")
    hits.sort(key=lambda t: (t[1]["wrong"], -t[1]["direct_ok"], -t[1]["answered"]))
    print(f"\n[grid] 满足 nullFC≥{want_null_fc} / Top-3≥{want_top3} / 答了≥{min_answered}：{len(hits)}")
    if not hits:
        return hits
    print(f"{'配置':<62} | {'Top-3':>7} {'nullFC':>6} {'d对':>3} {'d错':>3} "
          f"{'答了':>4} {'答错':>4}")
    for cfg, m in hits[:top]:
        brief = " ".join(f"{k}={cfg[k]}" for k in keys)
        print(f"{brief:<62} | {m['top3']:.4f} {m['null_fc']:.3f} "
              f"{m['direct_ok']:>3} {m['direct_wrong']:>3} "
              f"{m['answered']:>4} {m['wrong']:>4}")
    return hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", action="store_true", help="网格搜索（需先定 --base）")
    ap.add_argument("--source", action="store_true", help="用源码默认常量跑（校验落盘后的真实行为）")
    ap.add_argument("--sweep", help="t_fts | bm25 | vec | w | th | contain | match | all")
    ap.add_argument("--config", help="JSON 覆盖，如 '{\"T_FTS\":0.3}'")
    ap.add_argument("--base", help="JSON，作为所有 sweep 的基准（叠加在冻结基线之上）")
    ap.add_argument("--dump", action="store_true", help="导出逐题明细 JSON")
    ap.add_argument("--probe", action="append", default=[],
                    help="打印单题四路证据（可重复）")
    ap.add_argument("--probe-file", help="从文本文件逐行读题目做探针")
    ap.add_argument("--out", default=".workbuddy-ai/backup/calib-details.json")
    args = ap.parse_args()

    h = Harness()
    try:
        if args.source:
            # 不叠加冻结基线：直接用源码里落盘的那份常量跑，校验"真实行为"。
            h.base = None
        if args.base:
            h.base = {**BASELINE, **json.loads(args.base)}
            print(f"[base] {h.base}")
        if args.probe or args.probe_file:
            qs = list(args.probe)
            if args.probe_file:
                qs += [ln.strip() for ln in
                       Path(args.probe_file).read_text(encoding="utf-8").splitlines()
                       if ln.strip() and not ln.startswith("#")]
            _apply(BASELINE)
            for q in qs:
                probe(h, q)
            return
        if args.config:
            cfg = json.loads(args.config)
            s = h.run(cfg)
            print(f"配置 {cfg} → 实际 {s['config']}")
            print(table([(str(cfg), s)]))
            if args.dump:
                dump_details(s, ROOT / args.out, h.stats)
            return

        if args.grid:
            space = {
                "LINK_MODE": ["entity_flat", "entity_text", "entity_scaled"],
                "W_FIELD": [1.0, 2.0, 3.0, 4.0],
                "W_VEC": [1.0, 2.0, 3.0, 4.0],
                "TH_DIRECT": [0.60, 0.65, 0.70, 0.75],
                "TH_MAYBE": [0.30, 0.35, 0.40, 0.45],
            }
            grid_search(h, space)
            return

        if not args.sweep:
            s = h.run({})
            print(table([("默认常量", s)]))
            print(f"\n动作分布：{s['actions']}")
            print(f"语料：qa_docs={s['qa_docs']}  题目 {s['n']}"
                  f"（{s['scored']} scored + {s['null_items']} null）")
            if args.dump:
                dump_details(s, ROOT / args.out, h.stats)
            return

        grids = sweeps()
        names = list(grids) if args.sweep == "all" else [args.sweep]
        for name in names:
            if name not in grids:
                raise SystemExit(f"未知 sweep {name}；可选 {list(grids)}")
            rows = [(label, h.run(cfg)) for label, cfg in grids[name]]
            print(f"\n=== sweep {name} ===")
            print(table(rows))
    finally:
        h.close()


if __name__ == "__main__":
    main()
