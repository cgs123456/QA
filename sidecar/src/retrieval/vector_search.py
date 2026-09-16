"""语义向量检索（第 4 级，PRD §3.3）。

- 库：vec 表由调用方指定（默认 vec_qa_local；双 embedding 下为当前生效
  provider 对应的表）。表与向量维度强绑定（local 512 / cloud 3072），
  不可混用；表名经白名单校验后才拼 SQL。
- 距离：sqlite-vec L2（归一化向量下 d∈[0,2]）；s = 1 - d/2；
  门限 d < 0.6（→ s=0.7）；top5；过滤 rejected（R5）。
- store/rejected 过滤与 KNN 同一条 SQL（已验证 JOIN 形态）；
  k 取 oversample（过滤可能吃掉部分候选），阈值与截断在 Python 侧收口。
  初值，评测集标定。
"""

import sqlite_vec

VEC_DIM = 512
# P6 标定（2026-09-15）：0.6 → 0.8。放宽一档后 Top-3 0.8621→0.8736、
# direct 档答对 12→16，代价是 direct 档答错 0→1；继续放宽到 1.0 会让
# 对抗题的语义近邻进来（答错升到 3），故停在 0.8。见 docs/eval-final.md。
DIST_CUTOFF = 0.8
TOP_K = 5
KNN_OVERSAMPLE = 50


def normalize_score(distance: float) -> float:
    return 1.0 - distance / 2.0


def has_vectors(conn, store_id: str, table: str = "vec_qa_local") -> bool:
    """该 store 在 vec 表里**有没有向量**（用于区分"路不可用"与"路说没找到"）。

    空表 = 这个库还没建过向量（没跑 embedding / 重建未完成 / 本地模型缺失）。
    此时 vec 路返回空**不是**"答非所问"的证据，它的权重不该进融合分母 ——
    否则剩余三路的天花板只有 (Σw - w_vec)/Σw，w_vec 占大头时会跌破 TH_MAYBE，
    把"还没建索引"变成"一律拒答"。
    有向量、但本次全部低于门限，才是真的"没找到"，那是负证据，照常计分。
    """
    if table not in _KNOWN_TABLES:
        raise ValueError(f"未知 vec 表：{table}")
    row = conn.execute(
        f"SELECT 1 FROM {table} v JOIN qa_pairs q ON q.id = v.qa_id"
        " WHERE q.store_id = ? LIMIT 1",
        (store_id,),
    ).fetchone()
    return row is not None


def to_blob(vector) -> bytes:
    """接受 float 序列或已序列化 bytes，统一为 vec0 可用的 blob。"""
    if isinstance(vector, (bytes, bytearray, memoryview)):
        return bytes(vector)
    return sqlite_vec.serialize_float32([float(x) for x in vector])


_KNOWN_TABLES = ("vec_qa_local", "vec_qa_cloud")


def vector_search(conn, store_id: str, query_vector, top_k: int = TOP_K,
                  table: str = "vec_qa_local") -> list:
    """返回 [{qa_id, standard_question, official_answer, route='vec',
    distance, s}]，按 distance 升序。

    table：查哪张 vec 表（调用方传当前生效 provider 对应的表；未知表名
    大声 ValueError，绝不静默查错表）。维度错配的查询向量（如 512 向量查
    cloud 表）由 sqlite-vec 报错，调用方（answer_stream）将其降级为
    warnings，不断全链路。
    """
    if table not in _KNOWN_TABLES:
        raise ValueError(f"未知 vec 表：{table}")
    blob = to_blob(query_vector)
    k = max(KNN_OVERSAMPLE, top_k * 10)
    rows = conn.execute(
        "SELECT v.qa_id, v.distance, q.standard_question, q.official_answer,"
        " q.category"
        f" FROM {table} v JOIN qa_pairs q ON q.id = v.qa_id"
        " WHERE v.embedding MATCH ? AND k = ? AND q.store_id = ?"
        " AND (q.usage_status IS NULL OR q.usage_status <> 'rejected')"
        " ORDER BY v.distance ASC LIMIT ?",
        (blob, k, store_id, top_k),
    ).fetchall()
    hits = []
    for qa_id, distance, question, answer, category in rows:
        if distance is None or distance >= DIST_CUTOFF:
            continue
        hits.append(
            {
                "qa_id": qa_id,
                "standard_question": question,
                "official_answer": answer,
                "category": category,
                "route": "vec",
                "distance": distance,
                "s": normalize_score(distance),
            }
        )
    return hits
