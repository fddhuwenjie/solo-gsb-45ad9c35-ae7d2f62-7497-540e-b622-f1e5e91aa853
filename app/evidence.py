"""可复验的审查证据包：冻结审查快照、计算摘要、只读复验。

摘要只覆盖冻结载荷（目标日、有效权利要求含版本、问题），字段顺序固定，
不含 evidence_id、生成时间等易变字段；对同一输入重复生成必然得到相同摘要。

签发依据（防联动篡改）：签发时把 (evidence_id, sha256) 追加到数据库之外的
封存台账文件（与数据库同目录的 evidence_seals.jsonl，仅追加 JSON 行）。
复验时除「冻结字节 ↔ 库存摘要」外，还必须命中台账中该 evidence_id 对应的
原摘要。攻击者即使把库内 snapshot 与 sha256 一起替换为另一份合法快照，
也无法改写台账中的签发记录，替换内容不会被判为可信；无台账记录的历史旧包
一律按「无可靠签发依据」处理，不得仅凭库内摘要一致返回 verified=true。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from collections import OrderedDict

from .audit import run_audit
from .db import Database
from .errors import NotFoundError

# 封存台账文件名：与数据库同目录、独立于可改写的数据库记录，随数据卷持久化
SEAL_LEDGER_FILENAME = "evidence_seals.jsonl"

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


def _seal_ledger_path(db: Database) -> str:
    """封存台账路径：与数据库文件同目录，但不属于任何可改写的数据库记录。"""
    return os.path.join(os.path.dirname(os.path.abspath(db.path)), SEAL_LEDGER_FILENAME)


def _append_seal(db: Database, evidence_id: str, digest: str) -> None:
    """把签发依据追加到台账文件（调用方须已持有 db._lock）。"""
    record = json.dumps(
        {"evidence_id": evidence_id, "sha256": digest, "sealed_at": _utcnow()},
        ensure_ascii=False, separators=(",", ":"),
    )
    with open(_seal_ledger_path(db), "a", encoding="utf-8") as fh:
        fh.write(record + "\n")


def _load_seals(db: Database) -> dict[str, str] | None:
    """读取封存台账，返回 {evidence_id: 签发时封存的原摘要}。

    台账不存在视为空（全新部署或只有历史旧包）；容忍末行半写（追加过程中
    崩溃截断），但中间行损坏说明台账已被破坏，返回 None 让复验失败关闭。
    """
    path = _seal_ledger_path(db)
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    seals: dict[str, str] = {}
    last = len(lines) - 1
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            evidence_id = rec["evidence_id"]
            digest = rec["sha256"]
            if not (isinstance(evidence_id, str) and isinstance(digest, str)):
                raise ValueError("封存记录字段类型非法")
        except (ValueError, KeyError, TypeError):
            if i == last:
                break  # 末行容忍：视为该记录不存在
            return None
        seals[evidence_id] = digest
    return seals


def create_evidence(db: Database, application_number: str, target: dt.date) -> dict:
    """按现有 /audit 规则审查，冻结快照、建立签发依据并落库，返回 evidence_id 与摘要。"""
    audit_result = run_audit(db, application_number, target)
    payload = freeze_payload(audit_result)
    data = canonical_bytes(payload)
    digest = sha256_hex(data)
    evidence_id = uuid.uuid4().hex
    snapshot_text = data.decode("utf-8")

    with db._lock:
        # 先建立不依赖可改写数据库记录的签发依据，再落库；
        # 任何一步失败都不会产生「有库内记录却无签发依据」的新包。
        _append_seal(db, evidence_id, digest)
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
    """只读复验：同时核对签发依据、冻结字节与库存摘要，三者一致才判可信。

    绝不回查 revisions，因此后续修订不会改变旧包。三重核对：
    1. 冻结字节重算摘要 ↔ 库存摘要（单项篡改报告，保持既有行为）；
    2. 台账中的签发依据 ↔ 当前摘要（拦截 snapshot 与 sha256 被一起替换的
       联动篡改；无依据的历史旧包在此失败关闭）；
    3. 快照须为合法 JSON。
    """
    with db._lock:
        row = db.conn.execute(
            "SELECT evidence_id, application_number, target_date, snapshot, sha256"
            " FROM evidence_packages WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"证据包 '{evidence_id}' 不存在")
        seals = _load_seals(db)

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

    # 1) 冻结字节 ↔ 库存摘要：任一单点篡改在此暴露
    if recomputed != stored_digest:
        result["integrity_error"] = {
            "code": "SNAPSHOT_DIGEST_MISMATCH",
            "message": "快照已被篡改：重新计算的 SHA-256 与存储摘要不匹配",
            "stored_digest": stored_digest,
            "recomputed_digest": recomputed,
        }
        return result

    # 2) 签发依据：库内摘要再自洽，也必须与台账中封存的原摘要一致
    if seals is None:
        result["integrity_error"] = {
            "code": "EVIDENCE_SEAL_MISMATCH",
            "message": "签发依据台账已损坏，无法确认原内容，按不可信处理",
            "stored_digest": stored_digest,
            "recomputed_digest": recomputed,
        }
        return result
    sealed_digest = seals.get(row["evidence_id"])
    if sealed_digest is None:
        result["integrity_error"] = {
            "code": "EVIDENCE_SEAL_MISSING",
            "message": "无可靠签发依据（历史旧包或依据缺失），不得仅凭库内摘要一致判为可信",
            "stored_digest": stored_digest,
            "recomputed_digest": recomputed,
        }
        return result
    if sealed_digest != stored_digest:
        result["integrity_error"] = {
            "code": "EVIDENCE_SEAL_MISMATCH",
            "message": "库内快照与摘要被整体替换：与签发时封存的原摘要不符",
            "stored_digest": stored_digest,
            "recomputed_digest": recomputed,
        }
        return result

    # 3) 摘要与签发依据均一致，但快照仍须为合法 JSON
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
