"""审查逻辑：按目标日期重建有效版本并校验依附关系。"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field

from .db import Database
from .errors import NotFoundError


@dataclass
class EffectiveRevision:
    claim_number: int
    version: int
    claim_type: str
    reference_mode: str
    references: list[int]
    text: str
    effective_from: dt.date
    effective_to: dt.date | None
    # 该编号在库中的全部版本区间（用于说明引用为何无效）
    all_versions: list[dict] = field(default_factory=list)


def _load_effective(db: Database, application_number: str, target: dt.date):
    """返回 (有效版本映射, 全部版本区间映射)。有效判定：effective_from <= D < effective_to。"""
    with db._lock:
        rows = db.conn.execute(
            "SELECT claim_number, version, claim_type, reference_mode, refs, text,"
            " effective_from, effective_to FROM revisions"
            " WHERE application_number = ? ORDER BY claim_number, version",
            (application_number,),
        ).fetchall()

    effective: dict[int, EffectiveRevision] = {}
    history: dict[int, list[dict]] = {}
    for r in rows:
        ef = dt.date.fromisoformat(r["effective_from"])
        et = dt.date.fromisoformat(r["effective_to"]) if r["effective_to"] else None
        history.setdefault(r["claim_number"], []).append(
            {"version": r["version"], "effective_from": r["effective_from"],
             "effective_to": r["effective_to"]}
        )
        if ef <= target and (et is None or target < et):
            # 链式约束保证同一编号至多一个有效版本；防御性取最高版本
            effective[r["claim_number"]] = EffectiveRevision(
                claim_number=r["claim_number"], version=r["version"],
                claim_type=r["claim_type"], reference_mode=r["reference_mode"],
                references=json.loads(r["refs"]), text=r["text"],
                effective_from=ef, effective_to=et,
            )
    for cn, rev in effective.items():
        rev.all_versions = history.get(cn, [])
    return effective, history


def _find_cycles(graph: dict[int, list[int]]) -> list[list[int]]:
    """确定性 DFS 找环。返回去重后的环列表，每个环以最小编号开头。仅含有效节点间的边。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    seen: set[tuple] = set()
    cycles: list[list[int]] = []

    def canonical(cyc: list[int]) -> tuple:
        i = cyc.index(min(cyc))
        return tuple(cyc[i:] + cyc[:i])

    for start in sorted(graph):
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        path = [start]
        stack = [(start, iter(sorted(graph[start])))]
        while stack:
            node, it = stack[-1]
            descended = False
            for nxt in it:
                if nxt not in graph:
                    continue  # 指向无效节点的边不参与成环判定
                if color[nxt] == GRAY:
                    cyc = path[path.index(nxt):]
                    key = canonical(cyc)
                    if key not in seen:
                        seen.add(key)
                        cycles.append(list(key))
                elif color[nxt] == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append((nxt, iter(sorted(graph[nxt]))))
                    descended = True
                    break
            if not descended:
                color[node] = BLACK
                stack.pop()
                path.pop()
    cycles.sort()
    return cycles


def run_audit(db: Database, application_number: str, target: dt.date) -> dict:
    with db._lock:
        app = db.conn.execute(
            "SELECT application_number FROM applications WHERE application_number = ?",
            (application_number,),
        ).fetchone()
    if app is None:
        raise NotFoundError(f"申请号 '{application_number}' 不存在")

    effective, history = _load_effective(db, application_number, target)
    issues: list[dict] = []

    def chain_node(cn: int) -> dict:
        rev = effective.get(cn)
        return {"claim_number": cn, "version": rev.version if rev else None}

    # 1) 结构校验 + 引用有效性（交集/并集语义）
    for cn in sorted(effective):
        rev = effective[cn]
        if rev.claim_type == "independent":
            if rev.references:
                issues.append({
                    "code": "INDEPENDENT_WITH_REFERENCES",
                    "message": f"独立权利要求 {cn} 不得引用其他权利要求 {rev.references}",
                    "claim_number": cn, "claim_version": rev.version,
                    "chain": [chain_node(cn)] + [chain_node(r) for r in rev.references],
                })
            continue

        if not rev.references:
            issues.append({
                "code": "DEPENDENT_WITHOUT_REFERENCE",
                "message": f"从属权利要求 {cn} 未引用任何权利要求",
                "claim_number": cn, "claim_version": rev.version,
                "chain": [chain_node(cn)],
            })
            continue

        invalid = [r for r in rev.references if r not in effective]
        if rev.reference_mode == "all":
            # 交集语义：引用的每一项都必须有效
            for r in invalid:
                reason = _invalid_reason(r, history, target)
                issues.append({
                    "code": "INVALID_REFERENCE",
                    "message": f"从属权利要求 {cn}（交集引用）引用了无效项 {r}: {reason}",
                    "claim_number": cn, "claim_version": rev.version,
                    "chain": [chain_node(cn), chain_node(r)],
                })
        else:
            # 并集语义：至少一项有效即可
            if len(invalid) == len(rev.references):
                issues.append({
                    "code": "NO_VALID_REFERENCE",
                    "message": (f"从属权利要求 {cn}（并集引用）的任一引用 "
                                f"{rev.references} 在目标日均无效"),
                    "claim_number": cn, "claim_version": rev.version,
                    "chain": [chain_node(cn)] + [chain_node(r) for r in rev.references],
                })

    # 2) 成环检测（边 = 有效从属项 → 其引用的有效项）
    graph = {
        cn: [r for r in rev.references if r in effective]
        for cn, rev in effective.items()
        if rev.claim_type == "dependent"
    }
    for cyc in _find_cycles(graph):
        closed = cyc + [cyc[0]]
        issues.append({
            "code": "CYCLE",
            "message": "依附关系成环: " + " -> ".join(str(n) for n in closed),
            "claim_number": cyc[0],
            "claim_version": effective[cyc[0]].version,
            "chain": [chain_node(n) for n in closed],
        })

    # 确定性排序：编号 → 问题码 → 链上编号序列
    issues.sort(key=lambda i: (
        i["claim_number"], i["code"],
        tuple(n["claim_number"] for n in i["chain"]),
    ))

    return {
        "application_number": application_number,
        "target_date": target.isoformat(),
        "status": "fail" if issues else "pass",
        "effective_claims": [
            {
                "claim_number": rev.claim_number,
                "version": rev.version,
                "claim_type": rev.claim_type,
                "references": rev.references,
                "reference_mode": rev.reference_mode,
                "text": rev.text,
                "effective_from": rev.effective_from.isoformat(),
                "effective_to": rev.effective_to.isoformat() if rev.effective_to else None,
            }
            for _, rev in sorted(effective.items())
        ],
        "issues": issues,
    }


def _invalid_reason(ref: int, history: dict[int, list[dict]], target: dt.date) -> str:
    versions = history.get(ref)
    if not versions:
        return "该申请中不存在此编号"
    spans = [f"v{v['version']}[{v['effective_from']}~{v['effective_to'] or '开放'}]" for v in versions]
    return f"目标日期 {target.isoformat()} 不在其任何版本的有效区间内 ({', '.join(spans)})"
