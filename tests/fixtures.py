"""测试夹具：在内存中构造符合资料契约的最小数据集。

不修改 data/ 下的合成资料，所有业务规则的边界场景用小型定制数据验证；
全量合成资料上的口径测试放在 test_application.py。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.models import (
    AuthorizationEvent,
    GuaranteePool,
    LateEvidence,
    License,
    Release,
    RenewalRequest,
    Statement,
    Term,
    UsageEvent,
    Work,
)


def make_store(
    works=None,
    licenses=None,
    releases=None,
    events=None,
    statements=None,
    guarantees=None,
    terms=None,
    requests=None,
    auth_events=None,
    late_evidence=None,
):
    """构造与 MaterialStore 同接口的轻量内存仓库。"""
    work_map = {w.id: w for w in (works or [])}
    release_map = {r.id: r for r in (releases or [])}
    event_map = {e.origin_event: e for e in (events or [])}
    request_map = {r.id: r for r in (requests or [])}
    guarantee_map = {g.id: g for g in (guarantees or [])}
    term_map = {t.id: t for t in (terms or DEFAULT_TERMS)}
    return SimpleNamespace(
        works=work_map,
        licenses={lic.id: lic for lic in (licenses or [])},
        releases=release_map,
        events=event_map,
        statements=list(statements or []),
        guarantees=guarantee_map,
        terms=term_map,
        requests=request_map,
        authorization_events=list(auth_events or []),
        late_evidence=list(late_evidence or []),
    )


DEFAULT_TERMS = [
    Term("usage", "usage", 40, 0, None, ("guarantee",)),
    Term("capped", "period", 25, 80000, 150000, ()),
    Term("guarantee", "minimum", 20, 100000, None, ("usage",)),
]


def work(work_id, owner, parents=(), kind="music"):
    return Work(work_id, kind, tuple(parents), owner)


def license_(lic_id, work_id, customer, territories, purpose="release", frm="2026-01-01",
             to="2026-12-31", exclusive=False, sublicense=False, pool="GP0"):
    return License(lic_id, work_id, customer, tuple(territories), purpose, frm, to,
                   exclusive, sublicense, pool)


def release(rid, customer="C0", territory="EU", works=("W0",), packaged="2026-02-01",
            released=None, receipt=None, end=None):
    return Release(rid, customer, territory, tuple(works), packaged, released, receipt, end)


def event(origin, rid, stage="release", units=10, coverage=1.0, on="2026-02-01", disputed=False):
    return UsageEvent(origin, rid, on, stage, units, coverage, (origin + "a", origin + "b"), disputed)


def statement(sid, customer, quarter, territory, work_id, units, revision=1, received="2026-10-01"):
    return Statement(sid, customer, quarter, territory, work_id, units, revision, received)


def pool(pid="GP0", limit=500000, reserved=100000, works=("W0",), territories=("EU", "NA")):
    return GuaranteePool(pid, "USD", limit, reserved, tuple(works), tuple(territories))


def request_(rid, customer="C0", works=("W0",), territory="EU", purpose="release",
             frm="2027-01-01", to="2027-12-31", exclusive=False, group="ALT0",
             expires="2026-11-15"):
    return RenewalRequest(rid, customer, tuple(works), territory, purpose, frm, to,
                          exclusive, group, expires)


def auth(aid, rid, party, kind, on, work_id=None, territory="EU"):
    return AuthorizationEvent(aid, rid, party, kind, on, work_id, territory)
