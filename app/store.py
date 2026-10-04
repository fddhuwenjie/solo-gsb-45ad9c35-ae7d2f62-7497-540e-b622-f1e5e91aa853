"""写入路径：幂等 + 版本链校验 + 单事务落库。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import defaultdict

from .db import Database
from .errors import ConflictError, UnprocessableError
from .models import ApplicationIn


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _payload_hash(endpoint: str, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(f"{endpoint}\n{canonical}".encode("utf-8")).hexdigest()


def submit_revisions(db: Database, payload: ApplicationIn) -> tuple[dict, int, bool]:
    """提交一批修订。

    返回 (响应体, 状态码, 是否幂等重放)。
    - request_id 已存在且负载一致：返回首次存储的响应（重放，不重复写入）。
    - request_id 已存在但负载不同：409。
    - 任何校验失败：整体回滚，不写入部分数据。
    """
    endpoint = "POST /applications"
    body = payload.model_dump(mode="json")
    digest = _payload_hash(endpoint, body)

    with db._lock:
        row = db.conn.execute(
            "SELECT payload_hash, status_code, response_body FROM requests WHERE request_id = ?",
            (payload.request_id,),
        ).fetchone()
        if row is not None:
            if row["payload_hash"] != digest:
                raise ConflictError(
                    f"request_id '{payload.request_id}' 已使用且负载不一致",
                    details={"request_id": payload.request_id},
                )
            return json.loads(row["response_body"]), row["status_code"], True

        db.conn.execute("BEGIN IMMEDIATE")
        try:
            response = _apply_revisions(db, payload)
            status_code = 201
            db.conn.execute(
                "INSERT INTO requests (request_id, endpoint, payload_hash, status_code, response_body, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (payload.request_id, endpoint, digest, status_code,
                 json.dumps(response, ensure_ascii=False), _utcnow()),
            )
            db.conn.execute("COMMIT")
        except Exception:
            db.conn.execute("ROLLBACK")
            raise
        return response, status_code, False


def _apply_revisions(db: Database, payload: ApplicationIn) -> dict:
    conn = db.conn
    now = _utcnow()
    conn.execute(
        "INSERT OR IGNORE INTO applications (application_number, created_at) VALUES (?, ?)",
        (payload.application_number, now),
    )

    # 批次内按编号分组
    by_claim: dict[int, list] = defaultdict(list)
    for rev in payload.revisions:
        by_claim[rev.claim_number].append(rev)

    errors: list[dict] = []
    auto_closed: list[dict] = []
    plan_updates: list[tuple] = []   # (effective_to, app, claim_number, version)
    plan_inserts: list[tuple] = []

    for claim_number in sorted(by_claim):
        batch = by_claim[claim_number]
        # 批次内版本号不得重复
        versions = [r.version for r in batch]
        if len(set(versions)) != len(versions):
            errors.append({"claim_number": claim_number,
                           "message": f"批次内版本号重复: {sorted(versions)}"})
            continue
        batch.sort(key=lambda r: r.version)
        # 批次内版本号必须连续
        for prev, cur in zip(batch, batch[1:]):
            if cur.version != prev.version + 1:
                errors.append({"claim_number": claim_number,
                               "message": f"批次内版本链断裂: v{prev.version} 之后出现 v{cur.version}"})

        existing = conn.execute(
            "SELECT claim_number, version, effective_from, effective_to FROM revisions"
            " WHERE application_number = ? AND claim_number = ? ORDER BY version",
            (payload.application_number, claim_number),
        ).fetchall()

        expected_start = (existing[-1]["version"] + 1) if existing else 1
        if batch[0].version != expected_start:
            errors.append({
                "claim_number": claim_number,
                "message": f"版本链不可跳跃: 期望下一版本为 v{expected_start}，收到 v{batch[0].version}",
            })
            continue

        # 日期链：前一版本的失效日必须等于后一版本的生效日；开放版本自动封闭
        prev_from: dt.date | None = None
        prev_to: dt.date | None = None
        prev_version: int | None = None
        if existing:
            prev_from = dt.date.fromisoformat(existing[-1]["effective_from"])
            prev_to = (dt.date.fromisoformat(existing[-1]["effective_to"])
                       if existing[-1]["effective_to"] else None)
            prev_version = existing[-1]["version"]

        for rev in batch:
            if prev_version is not None:
                if rev.effective_from <= prev_from:
                    errors.append({
                        "claim_number": claim_number,
                        "message": (f"v{rev.version} 生效日 {rev.effective_from} 必须晚于 "
                                    f"v{prev_version} 生效日 {prev_from}"),
                    })
                elif prev_to is not None and rev.effective_from != prev_to:
                    errors.append({
                        "claim_number": claim_number,
                        "message": (f"替代链断裂: v{prev_version} 失效日为 {prev_to}，"
                                    f"v{rev.version} 生效日为 {rev.effective_from}，二者必须衔接"),
                    })
                elif prev_to is None:
                    # 自动封闭上一开放版本
                    plan_updates.append((rev.effective_from.isoformat(), payload.application_number,
                                         claim_number, prev_version))
                    auto_closed.append({"claim_number": claim_number,
                                        "closed_version": prev_version,
                                        "effective_to": rev.effective_from.isoformat()})
                    prev_to = rev.effective_from
            plan_inserts.append((
                payload.application_number, claim_number, rev.version, rev.claim_type.value,
                rev.reference_mode.value, json.dumps(rev.references), rev.text,
                rev.effective_from.isoformat(),
                rev.effective_to.isoformat() if rev.effective_to else None,
                payload.request_id, now,
            ))
            prev_from, prev_to, prev_version = rev.effective_from, rev.effective_to, rev.version

    if errors:
        raise UnprocessableError("修订链校验失败，未写入任何数据", details=errors)

    for effective_to, app_no, claim_number, version in plan_updates:
        conn.execute(
            "UPDATE revisions SET effective_to = ?"
            " WHERE application_number = ? AND claim_number = ? AND version = ?",
            (effective_to, app_no, claim_number, version),
        )
    conn.executemany(
        "INSERT INTO revisions (application_number, claim_number, version, claim_type,"
        " reference_mode, refs, text, effective_from, effective_to, request_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        plan_inserts,
    )

    return {
        "request_id": payload.request_id,
        "application_number": payload.application_number,
        "stored_revisions": len(plan_inserts),
        "auto_closed": auto_closed,
    }


def get_application(db: Database, application_number: str) -> dict | None:
    with db._lock:
        app = db.conn.execute(
            "SELECT application_number, created_at FROM applications WHERE application_number = ?",
            (application_number,),
        ).fetchone()
        if app is None:
            return None
        rows = db.conn.execute(
            "SELECT claim_number, version, claim_type, reference_mode, refs, text,"
            " effective_from, effective_to, request_id, created_at FROM revisions"
            " WHERE application_number = ? ORDER BY claim_number, version",
            (application_number,),
        ).fetchall()
    return {
        "application_number": app["application_number"],
        "created_at": app["created_at"],
        "revisions": [
            {**{k: r[k] for k in ("claim_number", "version", "claim_type", "reference_mode",
                                  "text", "effective_from", "effective_to", "request_id")},
             "references": json.loads(r["refs"])}
            for r in rows
        ],
    }
