"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class ClaimType(str, Enum):
    independent = "independent"  # 独立权利要求
    dependent = "dependent"      # 从属权利要求


class ReferenceMode(str, Enum):
    all = "all"  # 交集语义：引用“全部”所列权利要求（多项累计引用）
    any = "any"  # 并集语义：引用“任一”所列权利要求（择一引用）


class RevisionIn(BaseModel):
    """单条权利要求修订版本。"""

    claim_number: int = Field(ge=1, description="权利要求编号")
    version: int = Field(ge=1, description="版本号，同一编号内必须从 1 开始连续递增")
    claim_type: ClaimType
    references: list[int] = Field(default_factory=list, description="引用的权利要求编号")
    reference_mode: ReferenceMode = Field(
        default=ReferenceMode.all,
        description="多项引用语义：all=交集（全部引用），any=并集（任一引用）",
    )
    text: str = Field(min_length=1, description="权利要求文本")
    effective_from: dt.date = Field(description="生效日期（含当日）")
    effective_to: Optional[dt.date] = Field(default=None, description="失效日期（不含当日），null 表示开放")

    @field_validator("references")
    @classmethod
    def _normalize_refs(cls, v: list[int]) -> list[int]:
        for r in v:
            if r < 1:
                raise ValueError("引用编号必须为正整数")
        return sorted(set(v))

    @model_validator(mode="after")
    def _check(self) -> "RevisionIn":
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to 必须晚于 effective_from")
        if self.claim_type == ClaimType.independent and self.references:
            raise ValueError("独立权利要求不得引用其他权利要求")
        if self.claim_type == ClaimType.dependent and not self.references:
            raise ValueError("从属权利要求必须至少引用一项权利要求")
        if self.claim_number in self.references:
            raise ValueError("权利要求不得引用自身")
        return self


class ApplicationIn(BaseModel):
    """POST /applications 请求体。"""

    request_id: str = Field(min_length=1, max_length=128, description="幂等请求标识")
    application_number: str = Field(min_length=1, max_length=64, description="申请号")
    revisions: list[RevisionIn] = Field(min_length=1, description="本次提交的权利要求修订")


class AutoClosed(BaseModel):
    claim_number: int
    closed_version: int
    effective_to: dt.date


class ApplicationResponse(BaseModel):
    request_id: str
    application_number: str
    stored_revisions: int
    auto_closed: list[AutoClosed]


class AuditIn(BaseModel):
    """POST /audit 请求体。"""

    application_number: str = Field(min_length=1, max_length=64)
    target_date: dt.date = Field(description="重建有效版本的目标日期")


class ChainNode(BaseModel):
    claim_number: int
    version: Optional[int] = None  # 引用的编号不存在时为 null


class Issue(BaseModel):
    code: str
    message: str
    claim_number: int
    claim_version: Optional[int]
    chain: list[ChainNode] = Field(description="引用链（含各环节版本号）")


class EffectiveClaim(BaseModel):
    claim_number: int
    version: int
    claim_type: ClaimType
    references: list[int]
    reference_mode: ReferenceMode
    text: str
    effective_from: dt.date
    effective_to: Optional[dt.date]


class AuditResponse(BaseModel):
    application_number: str
    target_date: dt.date
    status: str  # "pass" | "fail"
    effective_claims: list[EffectiveClaim]
    issues: list[Issue]


class EvidenceCreatedResponse(BaseModel):
    evidence_id: str
    application_number: str
    target_date: dt.date
    sha256: str


class IntegrityError(BaseModel):
    code: str
    message: str
    stored_digest: Optional[str] = None
    recomputed_digest: Optional[str] = None


class FrozenSnapshot(BaseModel):
    """冻结载荷，字段顺序固定，不含生成时间等易变字段。"""

    target_date: dt.date
    effective_claims: list[EffectiveClaim]
    issues: list[Issue]


class EvidenceResponse(BaseModel):
    evidence_id: str
    application_number: str
    target_date: dt.date
    digest: str
    stored_digest: str
    verified: bool
    integrity_error: Optional[IntegrityError] = None
    snapshot: Optional[FrozenSnapshot] = None
