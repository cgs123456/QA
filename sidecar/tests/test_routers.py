"""routers 单测（HTTP）：compile/list/store CRUD/current 切换/检索跟随新库。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 与 test_auth.py / conftest.py 取值一致（同进程跑时互不覆盖）。
TEST_TOKEN = "test-token-" + "x" * 32


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}

SAMPLE_MD = """# 公司信息

## 公司成立时间

公司成立于2015年，总部位于北京。

## 发货周期

标准发货周期为三天内发出。

## 退货期限是几天？

七天内无理由退货，运费由买家承担。

## 客服电话是多少？

客服电话是400-123-4567。
"""

SAMPLE_JSON = """{
  "售后政策": {"退货期限": "七天内可退", "运费承担": "买家承担首重"},
  "qa_pairs": [
    {"standard_question": "发货周期是多久？", "official_answer": "三天内发出", "usage_status": "fixed"},
    {"question": "支持哪些支付方式？", "answer": "支持微信和支付宝"}
  ]
}"""


def test_auth_required(api_client):
    client, _ = api_client
    assert client.post("/knowledge/compile", json={}).status_code == 401
    assert client.get("/knowledge/search", params={"q": "x"}).status_code == 401
    assert client.get("/knowledge/list").status_code == 401


def test_compile_markdown_and_list(api_client):
    client, _ = api_client
    r = client.post(
        "/knowledge/compile",
        json={"store_name": "MD库", "format": "markdown", "content": SAMPLE_MD,
              "filename": "s.md"},
        headers=auth_headers(),
    )
    assert r.status_code == 200, r.text
    stats = r.json()["stats"]
    assert stats["qa_inserted"] == 2
    assert stats["fields_upserted"] == 2
    assert stats["aliases_written"] == 7

    stores = client.get("/knowledge/list", headers=auth_headers()).json()
    assert len(stores) == 1
    assert stores[0]["qa_count"] == 2
    assert stores[0]["field_count"] == 2


def test_compile_json(api_client):
    client, _ = api_client
    r = client.post(
        "/knowledge/compile",
        json={"store_name": "JSON库", "format": "json", "content": SAMPLE_JSON},
        headers=auth_headers(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["stats"]["qa_inserted"] == 2


def test_search_field_under_200ms_and_fts_hit(api_client):
    client, _ = api_client
    sid = client.post(
        "/knowledge/compile",
        json={"store_name": "检索库", "format": "markdown", "content": SAMPLE_MD},
        headers=auth_headers(),
    ).json()["store_id"]
    # 与 test_retrieval 相同的 3 文档语料（含 1 条 rejected，兼测 HTTP 过滤）。
    r = client.post(
        "/knowledge/compile",
        json={"store_id": sid, "format": "json", "content":
              '{"qa_pairs": [{"standard_question": "内部作废词XYZABC",'
              ' "official_answer": "作废", "usage_status": "rejected"}]}'},
        headers=auth_headers(),
    )
    assert r.json()["stats"]["qa_inserted"] == 1

    r = client.get(
        "/knowledge/search", params={"q": "多久发货", "store_id": sid},
        headers=auth_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["timings_ms"]["field"] < 200
    assert [h["field_name"] for h in body["field_hits"]] == ["发货周期"]

    r = client.get(
        "/knowledge/search", params={"q": "退货期限是几天", "store_id": sid},
        headers=auth_headers(),
    )
    assert any(
        h["standard_question"] == "退货期限是几天？" for h in r.json()["fts_hits"]
    )

    r = client.get(
        "/knowledge/search", params={"q": "内部作废词XYZABC", "store_id": sid},
        headers=auth_headers(),
    )
    assert r.json()["fts_hits"] == []


def test_store_crud_and_current_switch(api_client):
    client, _ = api_client
    s1 = client.post("/store", json={"name": "库A"}, headers=auth_headers()).json()
    s2 = client.post("/store", json={"name": "库B"}, headers=auth_headers()).json()

    client.post(
        "/knowledge/compile",
        json={"store_id": s1["id"], "format": "json", "content": SAMPLE_JSON},
        headers=auth_headers(),
    )
    client.post(
        "/knowledge/compile",
        json={"store_id": s2["id"], "format": "markdown", "content": SAMPLE_MD},
        headers=auth_headers(),
    )

    # 切到 B：无 store_id 的检索指向新库。
    r = client.put("/stores/current", json={"store_id": s2["id"]},
                   headers=auth_headers())
    assert r.status_code == 200
    assert r.json()["is_current"] is True
    body = client.get("/knowledge/search", params={"q": "多久发货"},
                      headers=auth_headers()).json()
    assert body["store_id"] == s2["id"]
    assert [x["field_name"] for x in body["field_hits"]] == ["发货周期"]

    # 切回 A：同问无字段命中（A 只有退货期限/运费承担字段）。
    client.put("/stores/current", json={"store_id": s1["id"]},
               headers=auth_headers())
    body = client.get("/knowledge/search", params={"q": "多久发货"},
                      headers=auth_headers()).json()
    assert body["store_id"] == s1["id"]
    assert body["field_hits"] == []

    # 详情与删除。
    detail = client.get(f"/store/{s1['id']}", headers=auth_headers()).json()
    assert detail["qa_count"] == 2
    assert client.delete(f"/store/{s1['id']}", headers=auth_headers()).status_code == 200
    assert client.get(f"/store/{s1['id']}", headers=auth_headers()).status_code == 404
    assert client.put("/stores/current", json={"store_id": s1["id"]},
                      headers=auth_headers()).status_code == 404
