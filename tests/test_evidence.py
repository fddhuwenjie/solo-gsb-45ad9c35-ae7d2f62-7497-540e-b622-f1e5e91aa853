"""审查证据包（POST/GET /audit/evidence）端到端测试。"""
import hashlib
import json

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


def make_evidence(client, app_no, target):
    return client.post("/audit/evidence",
                       json={"application_number": app_no, "target_date": target})


def seed_basic(client, app_no="CN2026-001", text2="如权利要求1所述的装置，包括中文"):
    """1(独立) + 2(从属于1)，均 2026-01-01 起开放。"""
    r = submit(client, "seed-1", app_no, [
        rev(1, 1, "independent", [], text="一种装置"),
        rev(2, 1, "dependent", [1], text=text2),
    ])
    assert r.status_code == 201, r.text


# ---------- 创建与只读复验 ----------

def test_create_and_read_evidence(client):
    seed_basic(client)
    created = make_evidence(client, "CN2026-001", "2026-03-01")
    assert created.status_code == 201, created.text
    body = created.json()
    assert len(body["evidence_id"]) == 32
    assert len(body["sha256"]) == 64
    assert body["target_date"] == "2026-03-01"

    got = client.get(f"/audit/evidence/{body['evidence_id']}")
    assert got.status_code == 200, got.text
    read_body = got.json()
    assert read_body["verified"] is True
    assert read_body["integrity_error"] is None
    assert read_body["digest"] == body["sha256"]
    assert read_body["stored_digest"] == body["sha256"]

    snap = read_body["snapshot"]
    assert snap["target_date"] == "2026-03-01"
    assert {c["claim_number"]: c["version"] for c in snap["effective_claims"]} == {1: 1, 2: 1}
    assert snap["issues"] == []


def test_snapshot_equals_audit_and_has_no_volatile_fields(client):
    submit(client, "s", "CN2026-101", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [1, 9], mode="all", text="引用 1 和 9"),
    ])
    audit_body = audit(client, "CN2026-101", "2026-03-01").json()
    created = make_evidence(client, "CN2026-101", "2026-03-01").json()
    snap = client.get(f"/audit/evidence/{created['evidence_id']}").json()["snapshot"]

    assert snap["target_date"] == audit_body["target_date"]
    assert snap["effective_claims"] == audit_body["effective_claims"]
    assert snap["issues"] == audit_body["issues"]
    # 不含生成时间、evidence_id、status 等摘要外字段
    assert set(snap.keys()) == {"target_date", "effective_claims", "issues"}
    assert "created_at" not in snap and "evidence_id" not in snap


def test_fixed_field_order_and_utf8(client):
    seed_basic(client)
    created = make_evidence(client, "CN2026-001", "2026-03-01").json()
    db = client.app.state.db
    with db._lock:
        raw = db.conn.execute(
            "SELECT snapshot FROM evidence_packages WHERE evidence_id = ?",
            (created["evidence_id"],),
        ).fetchone()["snapshot"]

    # UTF-8 原文重编码即得存储摘要
    assert hashlib.sha256(raw.encode("utf-8")).hexdigest() == created["sha256"]
    assert "中文" in raw  # ensure_ascii=False，中文不转义

    # 顶层固定字段顺序
    parsed = json.loads(raw)
    assert list(parsed.keys()) == [
        "target_date", "effective_claims", "issues"]
    assert list(parsed["effective_claims"][0].keys()) == [
        "claim_number", "version", "claim_type", "references",
        "reference_mode", "text", "effective_from", "effective_to"]


# ---------- 确定性 / 幂等摘要 ----------

def test_same_input_same_digest_distinct_ids(client):
    seed_basic(client)
    first = make_evidence(client, "CN2026-001", "2026-03-01").json()
    second = make_evidence(client, "CN2026-001", "2026-03-01").json()
    assert first["sha256"] == second["sha256"]
    assert first["evidence_id"] != second["evidence_id"]

    # 目标日是冻结载荷的一部分：不同目标日 -> 不同摘要（即使有效版本恰好相同）
    other = make_evidence(client, "CN2026-001", "2026-01-01").json()
    assert other["sha256"] != first["sha256"]


def test_digest_changes_when_claims_differ(client):
    seed_basic(client)
    before = make_evidence(client, "CN2026-001", "2026-03-01").json()
    submit(client, "r2", "CN2026-001",
           [rev(1, 2, "independent", [], text="一种改进装置", eff_from="2026-06-01")])
    after = make_evidence(client, "CN2026-001", "2026-07-01").json()
    assert before["sha256"] != after["sha256"]
    snap = client.get(f"/audit/evidence/{after['evidence_id']}").json()["snapshot"]
    assert {c["claim_number"]: c["version"] for c in snap["effective_claims"]} == {1: 2, 2: 1}


# ---------- 旧包不可变 ----------

def test_new_revisions_do_not_change_old_package(client):
    seed_basic(client)
    old = make_evidence(client, "CN2026-001", "2026-03-01").json()

    submit(client, "r2", "CN2026-001",
           [rev(1, 2, "independent", [], text="一种改进装置", eff_from="2026-06-01")])
    submit(client, "r3", "CN2026-001",
           [rev(2, 2, "dependent", [1], text="新版本从属项", eff_from="2027-01-01")])

    read_body = client.get(f"/audit/evidence/{old['evidence_id']}").json()
    assert read_body["verified"] is True
    assert read_body["digest"] == old["sha256"]
    snap = read_body["snapshot"]
    assert {c["claim_number"]: c["version"] for c in snap["effective_claims"]} == {1: 1, 2: 1}
    # 旧包冻结的是当时的审查结果：claim 2 v1 的失效日仍为开放 null
    assert snap["effective_claims"][1]["effective_to"] is None

    # 重新审查同一目标日反映自动封闭后的区间，但旧包字节与摘要不受影响
    live = audit(client, "CN2026-001", "2026-03-01").json()
    assert live["effective_claims"][1]["effective_to"] == "2027-01-01"
    still_old = client.get(f"/audit/evidence/{old['evidence_id']}").json()
    assert still_old["verified"] is True
    assert still_old["digest"] == old["sha256"]


# ---------- 篡改检测 ----------

def test_tampered_snapshot_reports_mismatch(client):
    seed_basic(client)
    created = make_evidence(client, "CN2026-001", "2026-03-01").json()
    db = client.app.state.db
    with db._lock:
        db.conn.execute(
            "UPDATE evidence_packages SET snapshot = ? WHERE evidence_id = ?",
            ('{"target_date":"2026-03-02","effective_claims":[],"issues":[]}',
             created["evidence_id"]),
        )

    read_body = client.get(f"/audit/evidence/{created['evidence_id']}").json()
    assert read_body["verified"] is False
    assert read_body["snapshot"] is None
    err = read_body["integrity_error"]
    assert err["code"] == "SNAPSHOT_DIGEST_MISMATCH"
    assert err["stored_digest"] == created["sha256"]
    assert err["recomputed_digest"] != created["sha256"]
    assert read_body["digest"] == err["recomputed_digest"]


def test_tampered_stored_digest_reports_mismatch(client):
    seed_basic(client)
    created = make_evidence(client, "CN2026-001", "2026-03-01").json()
    db = client.app.state.db
    with db._lock:
        db.conn.execute(
            "UPDATE evidence_packages SET sha256 = ? WHERE evidence_id = ?",
            ("0" * 64, created["evidence_id"]),
        )

    read_body = client.get(f"/audit/evidence/{created['evidence_id']}").json()
    assert read_body["verified"] is False
    assert read_body["integrity_error"]["code"] == "SNAPSHOT_DIGEST_MISMATCH"
    assert read_body["stored_digest"] == "0" * 64
    assert read_body["digest"] == created["sha256"]  # 重算值仍是原始摘要


# ---------- 联动篡改（替换快照 + 同步改写摘要） ----------

def test_coordinated_tamper_rejected(client):
    """保留 evidence_id，把快照换成另一份合法快照并同步改 sha256：
    冻结字节与库存摘要虽自洽，签发依据对不上，仍不得判为可信。"""
    seed_basic(client)
    victim = make_evidence(client, "CN2026-001", "2026-03-01").json()
    other = make_evidence(client, "CN2026-001", "2026-01-01").json()
    assert other["sha256"] != victim["sha256"]

    db = client.app.state.db
    with db._lock:
        foreign = db.conn.execute(
            "SELECT snapshot, sha256 FROM evidence_packages WHERE evidence_id = ?",
            (other["evidence_id"],),
        ).fetchone()
        db.conn.execute(
            "UPDATE evidence_packages SET snapshot = ?, sha256 = ? WHERE evidence_id = ?",
            (foreign["snapshot"], foreign["sha256"], victim["evidence_id"]),
        )

    r = client.get(f"/audit/evidence/{victim['evidence_id']}")
    assert r.status_code == 200, r.text
    read_body = r.json()
    # 后两者相符：重算摘要 == 库存摘要 == 替换后的新摘要
    assert read_body["digest"] == other["sha256"]
    assert read_body["stored_digest"] == other["sha256"]
    # 但仍报异常且快照不外发
    assert read_body["verified"] is False
    assert read_body["snapshot"] is None
    err = read_body["integrity_error"]
    assert err is not None
    assert err["code"] == "SEAL_MISMATCH"


def test_coordinated_tamper_with_foreign_seal_rejected(client):
    """连带复制另一包的 seal 也无法通过：seal 绑定 evidence_id，不可移植。"""
    seed_basic(client)
    victim = make_evidence(client, "CN2026-001", "2026-03-01").json()
    other = make_evidence(client, "CN2026-001", "2026-01-01").json()

    db = client.app.state.db
    with db._lock:
        foreign = db.conn.execute(
            "SELECT snapshot, sha256, seal FROM evidence_packages WHERE evidence_id = ?",
            (other["evidence_id"],),
        ).fetchone()
        db.conn.execute(
            "UPDATE evidence_packages SET snapshot = ?, sha256 = ?, seal = ?"
            " WHERE evidence_id = ?",
            (foreign["snapshot"], foreign["sha256"], foreign["seal"],
             victim["evidence_id"]),
        )

    read_body = client.get(f"/audit/evidence/{victim['evidence_id']}").json()
    assert read_body["verified"] is False
    assert read_body["snapshot"] is None
    assert read_body["integrity_error"]["code"] == "SEAL_MISMATCH"


def test_coordinated_tamper_leaves_other_packages_untouched(client):
    """联动篡改只影响被替换的包，其余包（含被复制来源）复验不受影响。"""
    seed_basic(client)
    victim = make_evidence(client, "CN2026-001", "2026-03-01").json()
    other = make_evidence(client, "CN2026-001", "2026-01-01").json()

    db = client.app.state.db
    with db._lock:
        foreign = db.conn.execute(
            "SELECT snapshot, sha256 FROM evidence_packages WHERE evidence_id = ?",
            (other["evidence_id"],),
        ).fetchone()
        db.conn.execute(
            "UPDATE evidence_packages SET snapshot = ?, sha256 = ? WHERE evidence_id = ?",
            (foreign["snapshot"], foreign["sha256"], victim["evidence_id"]),
        )

    assert client.get(f"/audit/evidence/{victim['evidence_id']}").json()["verified"] is False
    source = client.get(f"/audit/evidence/{other['evidence_id']}").json()
    assert source["verified"] is True
    assert source["digest"] == other["sha256"]


# ---------- 旧包（无签发依据） ----------

def test_legacy_package_without_seal_not_verified(client):
    """旧随机编号仍能读取，但无签发依据的旧包不得仅凭库内摘要一致返回 verified=true。"""
    seed_basic(client)
    created = make_evidence(client, "CN2026-001", "2026-03-01").json()
    db = client.app.state.db
    with db._lock:
        db.conn.execute(  # 模拟机制上线前签发的旧包：没有 seal
            "UPDATE evidence_packages SET seal = NULL WHERE evidence_id = ?",
            (created["evidence_id"],),
        )

    r = client.get(f"/audit/evidence/{created['evidence_id']}")
    assert r.status_code == 200, r.text
    read_body = r.json()
    assert read_body["evidence_id"] == created["evidence_id"]
    assert read_body["digest"] == created["sha256"]      # 库内摘要自洽
    assert read_body["stored_digest"] == created["sha256"]
    assert read_body["verified"] is False                # 但不得判为可信
    assert read_body["snapshot"] is None
    assert read_body["integrity_error"]["code"] == "SEAL_MISSING"


def test_legacy_package_single_tamper_report_still_works(client):
    """旧包发生单项篡改时，仍按 SNAPSHOT_DIGEST_MISMATCH 报告。"""
    seed_basic(client)
    created = make_evidence(client, "CN2026-001", "2026-03-01").json()
    db = client.app.state.db
    with db._lock:
        db.conn.execute(
            "UPDATE evidence_packages SET seal = NULL, snapshot = ? WHERE evidence_id = ?",
            ('{"target_date":"2026-03-02","effective_claims":[],"issues":[]}',
             created["evidence_id"]),
        )

    read_body = client.get(f"/audit/evidence/{created['evidence_id']}").json()
    assert read_body["verified"] is False
    assert read_body["snapshot"] is None
    assert read_body["integrity_error"]["code"] == "SNAPSHOT_DIGEST_MISMATCH"


def test_old_schema_database_is_migrated(tmp_path):
    """无 seal 列的旧版库打开后自动迁移；其中的旧包可读但不得 verified=true。"""
    import sqlite3

    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE evidence_packages (
            evidence_id        TEXT PRIMARY KEY,
            application_number TEXT NOT NULL,
            target_date        TEXT NOT NULL,
            snapshot           TEXT NOT NULL,
            sha256             TEXT NOT NULL,
            created_at         TEXT NOT NULL
        );
        """
    )
    snapshot = '{"target_date":"2026-03-01","effective_claims":[],"issues":[]}'
    digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO evidence_packages VALUES (?, ?, ?, ?, ?, ?)",
        ("a" * 32, "CN-OLD", "2026-03-01", snapshot, digest, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    app = create_app(db_path)
    with TestClient(app) as c:
        r = c.get("/audit/evidence/" + "a" * 32)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["digest"] == digest
        assert body["stored_digest"] == digest
        assert body["verified"] is False
        assert body["snapshot"] is None
        assert body["integrity_error"]["code"] == "SEAL_MISSING"
    app.state.db.close()


# ---------- 重启后仍可复验 ----------

def test_new_packages_verify_after_restart(tmp_path):
    """签发密钥持久化在库外密钥文件中：换进程重开同一库后，新旧包均可复验。"""
    db_path = str(tmp_path / "restart.db")
    app1 = create_app(db_path)
    with TestClient(app1) as c1:
        seed_basic(c1)
        created = make_evidence(c1, "CN2026-001", "2026-03-01").json()
    app1.state.db.close()

    app2 = create_app(db_path)  # 同一库文件，模拟服务重启
    with TestClient(app2) as c2:
        before = c2.get(f"/audit/evidence/{created['evidence_id']}").json()
        assert before["verified"] is True
        assert before["digest"] == created["sha256"]

        # 重启后签发的新包：同内容摘要稳定，且同样可复验
        created2 = make_evidence(c2, "CN2026-001", "2026-03-01").json()
        assert created2["sha256"] == created["sha256"]
        assert created2["evidence_id"] != created["evidence_id"]
        after = c2.get(f"/audit/evidence/{created2['evidence_id']}").json()
        assert after["verified"] is True
    app2.state.db.close()


def test_seal_not_exposed_in_responses(client):
    """签发依据只用于服务端复验，不出现在任何响应字段中。"""
    seed_basic(client)
    created = make_evidence(client, "CN2026-001", "2026-03-01").json()
    assert set(created.keys()) == {"evidence_id", "application_number", "target_date", "sha256"}
    read_body = client.get(f"/audit/evidence/{created['evidence_id']}").json()
    assert "seal" not in read_body


# ---------- 错误路径 ----------

def test_evidence_unknown_application(client):
    r = make_evidence(client, "NOPE", "2026-01-01")
    assert r.status_code == 404


def test_evidence_unknown_id(client):
    r = client.get("/audit/evidence/" + "f" * 32)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_existing_endpoints_still_work(client):
    """现有接口不受影响。"""
    seed_basic(client)
    assert client.get("/health").json() == {"status": "ok"}
    assert audit(client, "CN2026-001", "2026-03-01").json()["status"] == "pass"
