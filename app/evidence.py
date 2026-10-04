"""可复验的审查证据包：冻结审查快照、计算摘要、只读复验。

摘要只覆盖冻结载荷（目标日、有效权利要求含版本、问题），字段顺序固定，
不含 evidence_id、生成时间等易变字段；对同一输入重复生成必然得到相同摘要。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections import OrderedDict

from .audit import run_audit
from .db import Database
from .errors import NotFoundError

# 冻结载荷的顶层字段顺序（不得调整，历史快照依赖该顺序）
PAYLOAD_TOP_ORDER = ("target_date", "effective_claims", "issues")
CLAIM_FIELD_ORDER = (
    "claim_number", "version", "claim_type", "references",
    "reference_mode", "text", "effective_from", "effective_to",
)
ISSUE_FIELD_ORDER = ("code", "message", "claim_number", "claim_version", "chain")
CHAIN_NODE_FIELD_ORDER = ("claim_number", "version")


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def freeze_payload(audit_result: dict) -> OrderedDict:
    """按固定字段顺序构造冻结载荷（仅目标日、有效权利要求、问题）。"""
    payload: OrderedDict = OrderedDict()
    payload["target_date"] = audit_result["target_date"]
    payload["effective_claims"] = [
        OrderedDict((k, claim[k]) for k in CLAIM_FIELD_ORDER)
        for claim in audit_result["effective_claims"]
    ]
    payload["issues"] = [
        OrderedDict(
            [
                *(
                    (k, issue[k])
                    for k in ISSUE_FIELD_ORDER
                    if k != "chain"
                ),
                ("chain", [
                    OrderedDict((k, node[k]) for k in CHAIN_NODE_FIELD_ORDER)
                    for node in issue["chain"]
                ]),
            ]
        )
        for issue in audit_result["issues"]
    ]
    return payload


def canonical_bytes(payload: dict) -> bytes:
    """冻结载荷的规范序列化：紧凑、保留构造时的固定字段顺序、UTF-8。"""
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_evidence(db: Database, application_number: str, target: dt.date) -> dict:
    """按现有 /audit 规则审查，冻结快照并落库，返回 evidence_id 与摘要。"""
    audit_result = run_audit(db, application_number, target)
    payload = freeze_payload(audit_result)
    data = canonical_bytes(payload)
    digest = sha256_hex(data)
    evidence_id = uuid.uuid4().hex
    snapshot_text = data.decode("utf-8")

    with db._lock:
        db.conn.execute(
            "INSERT INTO evidence_packages"
            " (evidence_id, application_number, target_date, snapshot, sha256, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (evidence_id, application_number, target.isoformat(),
             snapshot_text, digest, _utcnow()),
        )

    return {
        "evidence_id": evidence_id,
        "application_number": application_number,
        "target_date": target.isoformat(),
        "sha256": digest,
    }


def read_evidence(db: Database, evidence_id: str) -> dict:
    """只读取冻结快照并复验：重新计算摘要、标记 verified。

    绝不回查 revisions，因此后续修订不会改变旧包；
    快照被篡改时通过 integrity_error 报告摘要不匹配。
    """
    with db._lock:
        row = db.conn.execute(
            "SELECT evidence_id, application_number, target_date, snapshot, sha256"
            " FROM evidence_packages WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"证据包 '{evidence_id}' 不存在")

    stored_text = row["snapshot"]
    stored_digest = row["sha256"]
    recomputed = sha256_hex(stored_text.encode("utf-8"))

    result = {
        "evidence_id": row["evidence_id"],
        "application_number": row["application_number"],
        "target_date": row["target_date"],
        "digest": recomputed,
        "stored_digest": stored_digest,
        "verified": False,
        "integrity_error": None,
        "snapshot": None,
    }

    if recomputed != stored_digest:
        result["integrity_error"] = {
            "code": "SNAPSHOT_DIGEST_MISMATCH",
            "message": "快照已被篡改：重新计算的 SHA-256 与存储摘要不匹配",
            "stored_digest": stored_digest,
            "recomputed_digest": recomputed,
        }
        return result

    try:
        snapshot = json.loads(stored_text)
    except (ValueError, TypeError):
        result["integrity_error"] = {
            "code": "SNAPSHOT_CORRUPT",
            "message": "摘要匹配但快照不是合法 JSON",
            "stored_digest": stored_digest,
            "recomputed_digest": recomputed,
        }
        return result

    result["verified"] = True
    result["snapshot"] = snapshot
    return result
