"""vector_search 单测（手工向量，确定性；同时覆盖 compiler embeddings 同事务位）。"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import sqlite_vec

from knowledge.compiler import compile_store
from knowledge.stores import create_store
from retrieval.vector_search import DIST_CUTOFF, normalize_score, vector_search


def _unit(i, dim=512):
    v = [0.0] * dim
    v[i] = 1.0
    return v


def _seed(db):
    store = create_store(db, "vec库")
    e1, e2 = _unit(0), _unit(1)
    e3 = [1.0 / math.sqrt(2), 1.0 / math.sqrt(2)] + [0.0] * 510
    qa = [
        {"id": "v1", "standard_question": "向量问题一", "official_answer": "答一",
         "usage_status": "fixed"},
        {"id": "v2", "standard_question": "向量问题二", "official_answer": "答二",
         "usage_status": "rejected"},
        {"id": "v3", "standard_question": "向量问题三", "official_answer": "答三",
         "usage_status": "fixed"},
    ]
    emb = {
        "v1": sqlite_vec.serialize_float32(e1),
        "v2": sqlite_vec.serialize_float32(e2),
        "v3": sqlite_vec.serialize_float32(e3),
    }
    stats = compile_store(db, store["id"], qa, [], embeddings=emb)
    assert stats["vec_written"] == 3
    assert db.execute("SELECT COUNT(*) FROM vec_qa_local").fetchone()[0] == 3
    return store["id"]


def test_self_match_distance_zero(db):
    sid = _seed(db)
    (top,) = vector_search(db, sid, sqlite_vec.serialize_float32(_unit(0)))[:1]
    assert top["qa_id"] == "v1"
    assert top["distance"] == 0.0
    assert top["s"] == 1.0
    assert top["route"] == "vec"


def test_rejected_excluded(db):
    sid = _seed(db)
    hits = vector_search(db, sid, sqlite_vec.serialize_float32(_unit(1)))
    assert all(h["qa_id"] != "v2" for h in hits)


def test_threshold_boundary(db):
    sid = _seed(db)
    # P6 标定后 DIST_CUTOFF=0.8：cos=0.65 → d≈0.84（外，v1 不得出现）；
    # cos=0.9 → d≈0.447（内，保留并验 s）。
    def qvec(cos):
        sin = math.sqrt(max(0.0, 1 - cos * cos))
        v = [0.0] * 512
        v[0], v[1] = cos, sin
        return sqlite_vec.serialize_float32(v)

    assert all(h["qa_id"] != "v1" for h in vector_search(db, sid, qvec(0.65)))
    inside = [h for h in vector_search(db, sid, qvec(0.9)) if h["qa_id"] == "v1"]
    assert inside and inside[0]["distance"] < DIST_CUTOFF
    assert abs(inside[0]["s"] - (1 - inside[0]["distance"] / 2)) < 1e-9


def test_far_query_returns_empty(db):
    sid = _seed(db)
    v = [-1.0] + [0.0] * 511
    assert vector_search(db, sid, sqlite_vec.serialize_float32(v)) == []


def test_normalize_score():
    assert normalize_score(0.0) == 1.0
    assert normalize_score(0.6) == 0.7  # 门限处 s=0.7
    assert normalize_score(2.0) == 0.0
