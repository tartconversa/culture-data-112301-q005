"""业务数据模型。

金额一律使用整数美分（minor units），与资料口径保持一致。
所有结果对象均可通过 :meth:`to_dict` 序列化为 JSON 友好结构。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


def _date(value: Optional[str]):
    """把 ISO 日期字符串转为 date，空值保持 None。"""
    if value is None:
        return None
    from datetime import date

    return date.fromisoformat(value)


@dataclass(frozen=True)
class Work:
    id: str
    kind: str
    parents: tuple[str, ...]
    owner: str


@dataclass(frozen=True)
class License:
    id: str
    work: str
    customer: str
    territories: tuple[str, ...]
    purpose: str
    from_date: str
    to_date: str
    exclusive: bool
    sublicense: bool
    guarantee_pool: str

    def covers(self, customer: str, work: str, territory: str, purpose: str, on_date: str) -> bool:
        """该许可是否在给定日期覆盖某客户/作品/地域/用途。"""
        return (
            self.customer == customer
            and self.work == work
            and territory in self.territories
            and self.purpose == purpose
            and self.from_date <= on_date <= self.to_date
        )


@dataclass(frozen=True)
class Release:
    id: str
    customer: str
    territory: str
    works: tuple[str, ...]
    packaged_on: str
    released_on: Optional[str]
    receipt_on: Optional[str]
    end_on: Optional[str]


@dataclass(frozen=True)
class UsageEvent:
    """按 origin_event 去重后的一次真实使用事件。"""

    origin_event: str
    release: str
    on: str
    stage: str
    observed_units: int
    coverage: float
    mirror_ids: tuple[str, ...]
    coverage_disputed: bool


@dataclass(frozen=True)
class Statement:
    id: str
    customer: str
    quarter: str
    territory: str
    work: str
    units: int
    revision: int
    received_on: str
    source: str = "data/statements.jsonl"


@dataclass(frozen=True)
class GuaranteePool:
    id: str
    currency: str
    limit_minor: int
    reserved_minor: int
    works: tuple[str, ...]
    territories: tuple[str, ...]


@dataclass(frozen=True)
class Term:
    id: str
    kind: str
    unit_minor: int
    minimum_minor: int
    cap_minor: Optional[int]
    allowed_with: tuple[str, ...]


@dataclass(frozen=True)
class RenewalRequest:
    id: str
    customer: str
    works: tuple[str, ...]
    territory: str
    purpose: str
    from_date: str
    to_date: str
    exclusive: bool
    alternative_group: str
    expires_on: str


@dataclass(frozen=True)
class AuthorizationEvent:
    id: str
    request: str
    party: str
    kind: str
    on: str
    work: Optional[str]
    territory: str


@dataclass(frozen=True)
class LateEvidence:
    id: str
    kind: str
    release: Optional[str]
    work: Optional[str]
    on: str
    value: str


# ---------------------------------------------------------------------------
# 业务结果对象
# ---------------------------------------------------------------------------

# 证据效力分级（由强到弱）
EVIDENCE_RELEASE_RECEIPT = "release_receipt"   # 正式发行回执
EVIDENCE_PACKAGE = "package_record"            # 打包记录
EVIDENCE_PREVIEW = "preview_only"              # 仅编辑预览
EVIDENCE_INSUFFICIENT = "insufficient"         # 证据不足/覆盖存疑


@dataclass
class WorkRight:
    """单条用量在某一作品上的权利覆盖结论。"""

    work: str
    owner_at_use: str
    is_derivative: bool
    license_ids: list[str] = field(default_factory=list)
    covered: bool = False
    reasons: list[str] = field(default_factory=list)


@dataclass
class ConsumptionLine:
    """一次去重使用事件的许可消耗解释。"""

    origin_event: str
    release: str
    customer: str
    territory: str
    on: str
    stage: str
    units: int
    evidence_strength: str
    coverage_disputed: bool
    billable: bool                  # 是否可计入正常许可收入
    revenue_minor: int              # 已授权部分的按量收入（美分，按历史许可口径暂以续约单价外的估值为 0 时由分析层补）
    effective_works: list[str]      # 该使用实际触及的作品（含可达原作）
    rights: list[WorkRight]
    conflicts: list[str] = field(default_factory=list)

    @property
    def all_rights_covered(self) -> bool:
        return all(r.covered for r in self.rights)


@dataclass
class BatchObservation:
    """发行批次的观察状态。"""

    release: str
    customer: str
    territory: str
    status: str                     # complete | observing
    packaged_on: str
    released_on: Optional[str]
    receipt_on: Optional[str]
    end_on: Optional[str]
    coverage_complete: bool
    reasons: list[str] = field(default_factory=list)
    observed_units: int = 0
    billable_units: int = 0
    unlicensed_units: int = 0
    coverage_disputed_units: int = 0


@dataclass
class StatementReconciliation:
    """季度声明与日志观察的对账结论（按最新修订）。"""

    customer: str
    quarter: str
    territory: str
    work: str
    latest_revision: int
    statement_units: int
    observed_units: int
    difference: int
    status: str                     # matched | overstated | understated | no_observation
    superseded_statement_ids: list[str] = field(default_factory=list)


@dataclass
class PlanQuote:
    """一个续约报价方案（单一条款或允许的条款组合）。"""

    plan_id: str
    request_id: str
    term_ids: list[str]
    term_kinds: list[str]
    projected_units: int
    usage_charge_minor: int
    minimum_minor: int
    cap_minor: Optional[int]
    total_minor: int
    guarantee_pool: Optional[str]
    guarantee_required_minor: int   # 需要担保池承载的最低保证部分
    valid_combo: bool
    blockers: list[str] = field(default_factory=list)


@dataclass
class OwnerAllocation:
    """可分配给单个权利人的收入区间。"""

    owner: str
    works: list[str]
    low_minor: int
    high_minor: int
    guaranteed_minor: int
    uncovered_liability_minor: int  # 无法被担保覆盖的责任
    reasons: list[str] = field(default_factory=list)


@dataclass
class RequestEvaluation:
    """续约请求在某一时点的完整评估。"""

    request_id: str
    status: str                     # 见 quoting.QUOTE_STATUSES
    formed_on: Optional[str]
    effective_license_id: Optional[str]
    customer_accepted_on: Optional[str]
    owner_confirmed_works: list[str]
    owner_pending_works: list[str]
    authorization_gaps: list[str]
    guarantee_pool: Optional[str]
    guarantee_available_minor: Optional[int]
    guarantee_required_minor: int
    guarantee_ok: bool
    exclusive_ok: bool
    expired: bool
    plan_quotes: list[PlanQuote]
    allocations: list[OwnerAllocation]
    blockers: list[str] = field(default_factory=list)


@dataclass
class AmendmentImpact:
    """资料修正对未来报价与既有许可的影响。"""

    amendment_id: str
    kind: str
    on: str
    subject: str
    future_risk: list[str]
    contract_actions: list[str]
    affected_licenses: list[str]


def to_dict(obj: Any) -> Any:
    """递归把数据类转为可 JSON 序列化的普通结构。"""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj
