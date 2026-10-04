"""可复验的审查证据包：冻结审查快照、计算摘要、只读复验。

摘要只覆盖冻结载荷（目标日、有效权利要求含版本、问题），字段顺序固定，
不含 evidence_id、生成时间等易变字段；对同一输入重复生成必然得到相同摘要。

签发时另行为每个包建立「签发依据」seal：用保存在数据库之外的密钥对
(evidence_id, application_number, target_date, sha256) 计算 HMAC-SHA256。
由于密钥不落库，篡改者即使把快照换成另一份合法快照并同步改写库内摘要，
也无法伪造与新内容匹配的 seal，复验时必然暴露。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
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


# ---------------------------------------------------------------------------
# 签发依据（seal）：不依赖可改写数据库记录的原内容校验依据
#
# 密钥只存在于数据库之外：优先取环境变量 EVIDENCE_SEAL_KEY；否则在数据库
# 文件旁生成并持久化一个随机密钥文件（evidence_seal.key，权限 0600），
# 保证重启后新包仍可复验。seal 本身虽随行入库，但没有库外密钥就无法为
# 替换后的内容伪造匹配值，因此校验依据不依赖库内记录的可信度。
# ---------------------------------------------------------------------------

SEAL_KEY_ENV = "EVIDENCE_SEAL_KEY"
SEAL_KEY_FILENAME = "evidence_seal.key"
_SEAL_DOMAIN = "evidence-seal-v1"


def _seal_key(db: Database) -> bytes:
    """加载（或首次生成并持久化）签发密钥；调用方须持有 db._lock。"""
    cached = getattr(db, "_evidence_seal_key", None)
    if cached is not None:
        return cached

    env_key = os.environ.get(SEAL_KEY_ENV, "").strip()
    if env_key:
        key = hashlib.sha256(f"{_SEAL_DOMAIN}\n{env_key}".encode("utf-8")).digest()
    else:
        key_path = os.path.join(
            os.path.dirname(os.path.abspath(db.path)), SEAL_KEY_FILENAME
        )
        if os.path.exists(key_path):
            with open(key_path, "r", encoding="ascii") as f:
                key = bytes.fromhex(f.read().strip())
        else:
            key = secrets.token_bytes(32)
            tmp_path = key_path + ".tmp"
            with open(tmp_path, "w", encoding="ascii") as f:
                f.write(key.hex())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, key_path)

    db._evidence_seal_key = key
    return key


def _seal_hex(key: bytes, evidence_id: str, application_number: str,
              target_date: str, digest: str) -> str:
    """对 (evidence_id, 申请号, 目标日, 内容摘要) 计算 HMAC-SHA256。

    绑定 evidence_id 使其他包的 seal 无法移植；绑定摘要即绑定冻结字节。
    """
    message = "\n".join([_SEAL_DOMAIN, evidence_id, application_number,
                         target_date, digest])
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


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
    """按现有 /audit 规则审查，冻结快照、建立签发依据并落库，返回 evidence_id 与摘要。"""
    audit_result = run_audit(db, application_number, target)
    payload = freeze_payload(audit_result)
    data = canonical_bytes(payload)
    digest = sha256_hex(data)
    evidence_id = uuid.uuid4().hex
    snapshot_text = data.decode("utf-8")

    with db._lock:
        seal = _seal_hex(_seal_key(db), evidence_id, application_number,
                         target.isoformat(), digest)
        db.conn.execute(
            "INSERT INTO evidence_packages"
            " (evidence_id, application_number, target_date, snapshot, sha256, seal, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (evidence_id, application_number, target.isoformat(),
             snapshot_text, digest, seal, _utcnow()),
        )

    return {
        "evidence_id": evidence_id,
        "application_number": application_number,
        "target_date": target.isoformat(),
        "sha256": digest,
    }


def read_evidence(db: Database, evidence_id: str) -> dict:
    """只读复验：同时核对签发依据、冻结字节与库存摘要，三者齐备才标记 verified。

    绝不回查 revisions，因此后续修订不会改变旧包。
    单项篡改（快照或摘要其一被改）报 SNAPSHOT_DIGEST_MISMATCH；
    联动篡改（快照与摘要被一并替换、彼此相符）因无法伪造签发依据，
    报 SEAL_MISMATCH；无签发依据的旧包报 SEAL_MISSING——
    后两种情况即使库内摘要自洽也绝不返回 verified=true。
    """
    with db._lock:
        row = db.conn.execute(
            "SELECT evidence_id, application_number, target_date, snapshot, sha256, seal"
            " FROM evidence_packages WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"证据包 '{evidence_id}' 不存在")
        key = _seal_key(db)

    stored_text = row["snapshot"]
    stored_digest = row["sha256"]
    stored_seal = row["seal"]
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

    # 1) 冻结字节 vs 库存摘要：任一被单独篡改都会在此暴露（原有行为不变）
    if recomputed != stored_digest:
        result["integrity_error"] = {
            "code": "SNAPSHOT_DIGEST_MISMATCH",
            "message": "快照已被篡改：重新计算的 SHA-256 与存储摘要不匹配",
            "stored_digest": stored_digest,
            "recomputed_digest": recomputed,
        }
        return result

    # 2) 签发依据缺失（机制上线前签发的旧包）：库内摘要一致不足以判定可信
    if not stored_seal:
        result["integrity_error"] = {
            "code": "SEAL_MISSING",
            "message": "该证据包没有可靠的签发依据，不能仅凭库内摘要一致判定可信",
            "stored_digest": stored_digest,
            "recomputed_digest": recomputed,
        }
        return result

    # 3) 签发依据核验：快照与摘要即使被联动替换（二者自洽），
    #    没有库外密钥也无法伪造与新内容匹配的 seal
    expected_seal = _seal_hex(key, row["evidence_id"], row["application_number"],
                              row["target_date"], stored_digest)
    if not hmac.compare_digest(expected_seal, stored_seal):
        result["integrity_error"] = {
            "code": "SEAL_MISMATCH",
            "message": "签发依据不匹配：快照与摘要可能被联动替换，内容不可信",
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
