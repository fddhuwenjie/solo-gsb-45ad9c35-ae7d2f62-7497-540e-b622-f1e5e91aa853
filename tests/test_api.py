"""端到端 API 测试。"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def rev(claim_number, version, claim_type="dependent", references=(1,),
        mode="all", text="文本", eff_from="2026-01-01", eff_to=None):
    return {
        "claim_number": claim_number,
        "version": version,
        "claim_type": claim_type,
        "references": list(references),
        "reference_mode": mode,
        "text": text,
        "effective_from": eff_from,
        "effective_to": eff_to,
    }


def submit(client, request_id, app_no, revisions):
    return client.post("/applications", json={
        "request_id": request_id,
        "application_number": app_no,
        "revisions": revisions,
    })


def audit(client, app_no, target):
    return client.post("/audit", json={"application_number": app_no, "target_date": target})


def seed_basic(client, app_no="CN2026-001"):
    """1(独立) + 2(从属于1)，均 2026-01-01 起开放。"""
    r = submit(client, "seed-1", app_no, [
        rev(1, 1, "independent", [], text="一种装置"),
        rev(2, 1, "dependent", [1], text="如权利要求1所述的装置"),
    ])
    assert r.status_code == 201, r.text


# ---------- 提交与版本链 ----------

def test_create_application_ok(client):
    seed_basic(client)
    state = client.get("/applications/CN2026-001").json()
    assert [r["claim_number"] for r in state["revisions"]] == [1, 2]


def test_version_chain_cannot_skip(client):
    seed_basic(client)
    r = submit(client, "r2", "CN2026-001", [rev(1, 3, "independent", [], eff_from="2026-06-01")])
    assert r.status_code == 422
    assert "不可跳跃" in r.json()["error"]["details"][0]["message"]


def test_version_chain_must_start_at_one(client):
    r = submit(client, "r1", "CN2026-002", [rev(1, 2, "independent", [])])
    assert r.status_code == 422


def test_auto_close_open_version_and_reconstruct(client):
    seed_basic(client)
    r = submit(client, "r2", "CN2026-001",
               [rev(1, 2, "independent", [], text="一种改进装置", eff_from="2026-06-01")])
    assert r.status_code == 201
    assert r.json()["auto_closed"] == [
        {"claim_number": 1, "closed_version": 1, "effective_to": "2026-06-01"}]

    old = audit(client, "CN2026-001", "2026-03-01").json()
    assert {c["claim_number"]: c["version"] for c in old["effective_claims"]} == {1: 1, 2: 1}
    new = audit(client, "CN2026-001", "2026-07-01").json()
    assert {c["claim_number"]: c["version"] for c in new["effective_claims"]} == {1: 2, 2: 1}


def test_date_chain_gap_rejected(client):
    seed_basic(client)
    r = submit(client, "r2", "CN2026-001", [
        rev(1, 2, "independent", [], eff_from="2026-06-01", eff_to="2026-12-31"),
    ])
    assert r.status_code == 201
    # v3 生效日必须衔接 v2 失效日 2026-12-31
    r = submit(client, "r3", "CN2026-001",
               [rev(1, 3, "independent", [], eff_from="2027-02-01")])
    assert r.status_code == 422
    assert "替代链断裂" in r.json()["error"]["details"][0]["message"]


def test_failed_batch_writes_nothing(client):
    r = submit(client, "bad", "CN2026-009", [
        rev(1, 1, "independent", []),
        rev(2, 5, "dependent", [1]),  # 版本跳跃 -> 整批失败
    ])
    assert r.status_code == 422
    assert client.get("/applications/CN2026-009").status_code == 404
    assert audit(client, "CN2026-009", "2026-01-15").status_code == 404


# ---------- 幂等 ----------

def test_idempotent_replay(client):
    seed_basic(client)
    body = {"request_id": "r2", "application_number": "CN2026-001",
            "revisions": [rev(1, 2, "independent", [], eff_from="2026-06-01")]}
    first = client.post("/applications", json=body)
    second = client.post("/applications", json=body)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["X-Idempotent-Replay"] == "true"
    state = client.get("/applications/CN2026-001").json()
    assert len([r for r in state["revisions"] if r["claim_number"] == 1]) == 2  # 未重复写入


def test_request_id_conflict(client):
    seed_basic(client)
    client.post("/applications", json={"request_id": "dup", "application_number": "CN2026-001",
                                       "revisions": [rev(1, 2, "independent", [], eff_from="2026-06-01")]})
    r = client.post("/applications", json={"request_id": "dup", "application_number": "CN2026-001",
                                           "revisions": [rev(1, 2, "independent", [], eff_from="2026-07-01")]})
    assert r.status_code == 409


# ---------- 审查：引用有效性 / 交集并集 / 成环 ----------

def test_audit_pass(client):
    seed_basic(client)
    r = audit(client, "CN2026-001", "2026-03-01")
    assert r.status_code == 200
    assert r.json()["status"] == "pass"
    assert r.json()["issues"] == []


def test_invalid_reference_intersection(client):
    submit(client, "s", "CN2026-101", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [1, 9], mode="all"),
    ])
    issues = audit(client, "CN2026-101", "2026-03-01").json()["issues"]
    assert [i["code"] for i in issues] == ["INVALID_REFERENCE"]
    assert issues[0]["chain"] == [{"claim_number": 2, "version": 1},
                                  {"claim_number": 9, "version": None}]


def test_union_semantics(client):
    submit(client, "s", "CN2026-102", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [1, 9], mode="any"),   # 并集：1 有效即可
        rev(3, 1, "dependent", [8, 9], mode="any"),   # 并集：全部无效
    ])
    body = audit(client, "CN2026-102", "2026-03-01").json()
    assert [i["code"] for i in body["issues"]] == ["NO_VALID_REFERENCE"]
    assert body["issues"][0]["claim_number"] == 3


def test_reference_not_effective_at_target_date(client):
    submit(client, "s", "CN2026-103", [
        rev(1, 1, "independent", [], eff_from="2026-06-01"),
        rev(2, 1, "dependent", [1]),
    ])
    # 2026-03-01 时权利要求 1 尚未生效
    issues = audit(client, "CN2026-103", "2026-03-01").json()["issues"]
    assert [i["code"] for i in issues] == ["INVALID_REFERENCE"]
    assert "有效区间" in issues[0]["message"]


def test_cycle_detection(client):
    submit(client, "s", "CN2026-104", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [3]),
        rev(3, 1, "dependent", [2]),
    ])
    issues = audit(client, "CN2026-104", "2026-03-01").json()["issues"]
    cycles = [i for i in issues if i["code"] == "CYCLE"]
    assert len(cycles) == 1
    assert [n["claim_number"] for n in cycles[0]["chain"]] == [2, 3, 2]
    assert [n["version"] for n in cycles[0]["chain"]] == [1, 1, 1]


def test_deterministic_issue_order(client):
    submit(client, "s", "CN2026-105", [
        rev(1, 1, "independent", []),
        rev(5, 1, "dependent", [9]),
        rev(3, 1, "dependent", [8]),
    ])
    issues = audit(client, "CN2026-105", "2026-03-01").json()["issues"]
    assert [i["claim_number"] for i in issues] == [3, 5]


def test_audit_unknown_application(client):
    assert audit(client, "NOPE", "2026-01-01").status_code == 404
