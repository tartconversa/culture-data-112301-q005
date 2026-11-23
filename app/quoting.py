"""报价生命周期、授权校验、独占性与共享担保占用。

状态机（每个续约请求一个报价对象，状态只追加、可重放）：

    draft ──issue──► pending ──条件齐备──► formed（有效许可）
                       │                    │
                       ├──expire──► expired │
                       └──withdraw──► withdrawn

成立条件（与到达次序无关，按事件日期重放判定）：

1. 报价未过期（事件日不晚于 expires_on；过期后到达的授权不使报价复活）；
2. 客户已接受；
3. 范围内每个作品在目标地域都取得对应权利人的 owner_confirm；
4. 独占请求不与既有独占许可或同地域同作品的其他已成立报价冲突；
5. 含保底的方案在对应共享担保池中仍有额度（reserved + 其他报价占用）。

同一客户的互斥组（alternative_group）只能有一个 formed；formed 的条件承诺
不被后来更优方案替换。过期报价立即释放预占额度，不占用后续报价。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from .lineage import Lineage
from .models import AuthorizationEvent, OwnerAllocation, PlanQuote, RenewalRequest
from .pricing import RenewalAnalyzer
from .store import MaterialStore

QUOTE_DRAFT = "draft"
QUOTE_PENDING = "pending"
QUOTE_FORMED = "formed"
QUOTE_EXPIRED = "expired"
QUOTE_WITHDRAWN = "withdrawn"

TERMINAL_STATUSES = {QUOTE_FORMED, QUOTE_EXPIRED, QUOTE_WITHDRAWN}


@dataclass
class QuoteRecord:
    request_id: str
    status: str = QUOTE_DRAFT
    plan_id: Optional[str] = None
    issued_on: Optional[str] = None
    customer_accepted_on: Optional[str] = None
    owner_confirmed_works: list[str] = field(default_factory=list)
    owner_pending_works: list[str] = field(default_factory=list)
    formed_on: Optional[str] = None
    effective_license_id: Optional[str] = None
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "plan_id": self.plan_id,
            "issued_on": self.issued_on,
            "customer_accepted_on": self.customer_accepted_on,
            "owner_confirmed_works": sorted(self.owner_confirmed_works),
            "owner_pending_works": sorted(self.owner_pending_works),
            "formed_on": self.formed_on,
            "effective_license_id": self.effective_license_id,
            "history": self.history,
        }


class QuoteManager:
    def __init__(self, store: MaterialStore, lineage: Lineage, analyzer: RenewalAnalyzer):
        self.store = store
        self.lineage = lineage
        self.analyzer = analyzer
        self.sink = None
        self.quotes: dict[str, QuoteRecord] = {
            rid: QuoteRecord(rid) for rid in store.requests
        }
        self._events_by_request: dict[str, list[AuthorizationEvent]] = defaultdict(list)
        for event in store.authorization_events:
            self._events_by_request[event.request].append(event)

    # ------------------------------------------------------------------
    # 账本重放
    # ------------------------------------------------------------------
    def replay(self, ledger_entries: list[dict]) -> None:
        for entry in ledger_entries:
            kind = entry["action"]
            if kind == "issue":
                self.issue(entry["request_id"], entry["plan_id"], entry["on"], record=False)
            elif kind == "expire":
                self.expire(entry["request_id"], entry["on"], record=False)
            elif kind == "withdraw":
                self.withdraw(entry["request_id"], entry["on"], record=False)

    def _log(self, quote: QuoteRecord, action: str, on: str, note: str = "") -> None:
        quote.history.append({"action": action, "on": on, "note": note})

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def issue(self, request_id: str, plan_id: str, on: str, record: bool = True) -> QuoteRecord:
        quote = self.quotes[request_id]
        if quote.status != QUOTE_DRAFT:
            raise ValueError(f"报价 {request_id} 已处于 {quote.status}，不能再次签发")
        request = self.store.requests[request_id]
        if on > request.expires_on:
            quote.status = QUOTE_EXPIRED
            self._log(quote, "expire", on, "issued_after_expiry")
            return quote
        quote.status = QUOTE_PENDING
        quote.plan_id = plan_id
        quote.issued_on = on
        self._log(quote, "issue", on, plan_id)
        self._apply_authorization_events(quote, request)
        if record:
            self._persist("issue", request_id, on, plan_id)
        return quote

    def expire(self, request_id: str, on: str, record: bool = True) -> QuoteRecord:
        quote = self.quotes[request_id]
        if quote.status in TERMINAL_STATUSES:
            return quote
        quote.status = QUOTE_EXPIRED
        self._log(quote, "expire", on)
        if record:
            self._persist("expire", request_id, on)
        return quote

    def withdraw(self, request_id: str, on: str, record: bool = True) -> QuoteRecord:
        quote = self.quotes[request_id]
        if quote.status == QUOTE_FORMED:
            raise ValueError(f"报价 {request_id} 已形成有效许可，不能撤回")
        if quote.status in TERMINAL_STATUSES:
            return quote
        quote.status = QUOTE_WITHDRAWN
        self._log(quote, "withdraw", on)
        if record:
            self._persist("withdraw", request_id, on)
        return quote

    def _persist(self, action: str, request_id: str, on: str, plan_id: str | None = None) -> None:
        # 延迟绑定 StateStore：CLI 负责注入 sink
        if self.sink is not None:
            self.sink.append({"domain": "quote", "action": action, "request_id": request_id,
                              "plan_id": plan_id, "on": on})

    def attach_sink(self, sink) -> None:
        self.sink = sink

    # ------------------------------------------------------------------
    # 授权事件（资料中的原始动作，不预先判定合同）
    # ------------------------------------------------------------------
    def _apply_authorization_events(self, quote: QuoteRecord, request: RenewalRequest) -> None:
        for event in sorted(self._events_by_request.get(request.id, []), key=lambda e: e.on):
            self._ingest_event(quote, request, event)
        self._try_form(quote, request)

    def _ingest_event(self, quote: QuoteRecord, request: RenewalRequest, event: AuthorizationEvent) -> None:
        if event.on > request.expires_on:
            self._log(quote, "event_ignored_expired", event.on, event.id)
            return
        if event.kind == "customer_accept":
            if quote.customer_accepted_on is None or event.on < quote.customer_accepted_on:
                quote.customer_accepted_on = event.on
            self._log(quote, "customer_accept", event.on, event.id)
            return
        if not event.work:
            return
        # 授权方必须是事件日适用的权利人；归属裁决前旧权利人的确认只在裁决日前有效
        required_owner = self.lineage.owner_on(event.work, event.on)
        if event.party != required_owner:
            self._log(
                quote, "owner_event_wrong_party", event.on,
                f"{event.id}:{event.work}:party={event.party},owner={required_owner}",
            )
            return
        if event.kind == "owner_confirm":
            if event.work not in quote.owner_confirmed_works:
                quote.owner_confirmed_works.append(event.work)
            if event.work in quote.owner_pending_works:
                quote.owner_pending_works.remove(event.work)
            self._log(quote, "owner_confirm", event.on, f"{event.id}:{event.work}")
        elif event.kind == "owner_pending":
            if event.work not in quote.owner_pending_works and event.work not in quote.owner_confirmed_works:
                quote.owner_pending_works.append(event.work)
            self._log(quote, "owner_pending", event.on, f"{event.id}:{event.work}")

    # ------------------------------------------------------------------
    # 成立判定
    # ------------------------------------------------------------------
    def required_works(self, request: RenewalRequest) -> list[str]:
        wanted: list[str] = []
        for work_id in request.works:
            for wid in self.lineage.effective_works(work_id):
                if wid not in wanted:
                    wanted.append(wid)
        return wanted

    def _confirm_date(self, quote: QuoteRecord, work_id: str) -> str:
        for entry in reversed(quote.history):
            if entry["action"] == "owner_confirm" and entry["note"].endswith(":" + work_id):
                return entry["on"]
        return "0000-01-01"

    def _guarantee_reservations(self, exclude_request: str | None = None) -> dict[str, int]:
        """当前所有 formed/pending 报价对担保池的额外占用。"""
        reserved: dict[str, int] = defaultdict(int)
        for quote in self.quotes.values():
            if quote.request_id == exclude_request:
                continue
            if quote.status not in (QUOTE_PENDING, QUOTE_FORMED) or not quote.plan_id:
                continue
            request = self.store.requests[quote.request_id]
            plan = self._plan_unreserved(quote)
            if plan and plan.guarantee_required_minor:
                pool_id = self.analyzer._pool_for(request)
                if pool_id:
                    reserved[pool_id] += plan.guarantee_required_minor
        return reserved

    def _plan_unreserved(self, quote: QuoteRecord) -> Optional[PlanQuote]:
        """按担保池基线（不含其他报价占用）取方案，用于占用统计。"""
        if not quote.plan_id:
            return None
        request = self.store.requests[quote.request_id]
        for plan in self.analyzer.plan_quotes(request):
            if plan.plan_id == quote.plan_id:
                return plan
        return None

    def _plan(self, quote: QuoteRecord) -> Optional[PlanQuote]:
        if not quote.plan_id:
            return None
        request = self.store.requests[quote.request_id]
        for plan in self.analyzer.plan_quotes(
            request, extra_reserved=self._guarantee_reservations(exclude_request=request.id)
        ):
            if plan.plan_id == quote.plan_id:
                return plan
        return None

    def _exclusive_conflict(self, request: RenewalRequest, quote: QuoteRecord) -> Optional[str]:
        # 同一客户互斥组只能有一个 formed 报价（与独占要求无关，始终生效）
        for other in self.quotes.values():
            if other.request_id == request.id or other.status != QUOTE_FORMED:
                continue
            other_req = self.store.requests[other.request_id]
            if other_req.alternative_group == request.alternative_group and other_req.customer == request.customer:
                return f"alternative_group_formed_quote:{other.request_id}"
        if not request.exclusive:
            return None
        for work_id in self.required_works(request):
            # 既有独占许可冲突
            for lic in self.store.licenses.values():
                if (
                    lic.work == work_id
                    and lic.customer != request.customer
                    and lic.exclusive
                    and lic.purpose == request.purpose
                    and request.territory in lic.territories
                    and lic.from_date <= request.from_date <= lic.to_date
                ):
                    return f"existing_exclusive_license:{lic.id}:{work_id}"
            # 同地域同作品的其他已成立报价
            for other in self.quotes.values():
                if other.request_id == request.id or other.status != QUOTE_FORMED:
                    continue
                other_req = self.store.requests[other.request_id]
                if other_req.territory == request.territory and work_id in self.required_works(other_req):
                    return f"formed_quote_conflict:{other.request_id}:{work_id}"
        return None

    def _try_form(self, quote: QuoteRecord, request: RenewalRequest) -> None:
        if quote.status != QUOTE_PENDING:
            return
        required = self.required_works(request)
        confirmed = set(quote.owner_confirmed_works)
        still_pending = [w for w in required if w not in confirmed]
        quote.owner_pending_works = sorted(set(quote.owner_pending_works) | set(still_pending) - confirmed)
        blockers: list[str] = []
        if quote.customer_accepted_on is None:
            blockers.append("awaiting_customer_accept")
        # 归属裁决：许可起日适用的权利人与确认日权利人不同则需补授权
        for work_id in still_pending:
            blockers.append(f"awaiting_owner_confirmation:{work_id}")
        for work_id in confirmed:
            owner_at_start = self.lineage.owner_on(work_id, request.from_date)
            owner_at_confirm = self.lineage.owner_on(
                work_id,
                self._confirm_date(quote, work_id),
            )
            if owner_at_start != owner_at_confirm:
                blockers.append(
                    f"ownership_ruling_requires_new_owner_confirmation:{work_id}:{owner_at_start}"
                )
        plan = self._plan(quote)
        if plan is not None and not plan.valid_combo:
            blockers.extend(plan.blockers)
        exclusive_issue = self._exclusive_conflict(request, quote)
        if exclusive_issue:
            blockers.append(exclusive_issue)
        if not blockers:
            quote.status = QUOTE_FORMED
            accept_dates = [d for d in (quote.issued_on, quote.customer_accepted_on) if d]
            quote.formed_on = max(accept_dates) if accept_dates else quote.issued_on
            quote.effective_license_id = f"EL-{request.id}"
            self._log(quote, "formed", quote.formed_on, quote.effective_license_id)
        else:
            self._log(quote, "pending_check", request.expires_on, ";".join(blockers))

    # ------------------------------------------------------------------
    # 时点评估（供查询输出）
    # ------------------------------------------------------------------
    def evaluate(self, request_id: str) -> dict:
        request = self.store.requests[request_id]
        quote = self.quotes[request_id]
        projection = self.analyzer.project(request)
        plans = self.analyzer.plan_quotes(
            request, extra_reserved=self._guarantee_reservations(exclude_request=request_id)
        )
        chosen = next((p for p in plans if p.plan_id == quote.plan_id), None)
        allocations: list[OwnerAllocation] = []
        if chosen is not None:
            allocations = self.analyzer.allocations(request, chosen, projection)
        required = self.required_works(request)
        confirmed = set(quote.owner_confirmed_works)
        gaps = [w for w in required if w not in confirmed]
        pool_id = self.analyzer._pool_for(request)
        pool = self.store.guarantees.get(pool_id) if pool_id else None
        reservations = self._guarantee_reservations(exclude_request=request_id)
        available = None
        if pool:
            available = pool.limit_minor - pool.reserved_minor - reservations.get(pool_id, 0)
        blockers = []
        if quote.status == QUOTE_PENDING:
            if quote.customer_accepted_on is None:
                blockers.append("awaiting_customer_accept")
            for work_id in gaps:
                blockers.append(f"awaiting_owner_confirmation:{work_id}")
            for work_id in confirmed:
                owner_at_start = self.lineage.owner_on(work_id, request.from_date)
                owner_at_confirm = self.lineage.owner_on(work_id, self._confirm_date(quote, work_id))
                if owner_at_start != owner_at_confirm:
                    blockers.append(
                        f"ownership_ruling_requires_new_owner_confirmation:{work_id}:{owner_at_start}"
                    )
            if chosen is not None and not chosen.valid_combo:
                blockers.extend(chosen.blockers)
            issue = self._exclusive_conflict(request, quote)
            if issue:
                blockers.append(issue)
        return {
            "request_id": request_id,
            "alternative_group": request.alternative_group,
            "status": quote.status,
            "formed_on": quote.formed_on,
            "effective_license_id": quote.effective_license_id,
            "customer_accepted_on": quote.customer_accepted_on,
            "expires_on": request.expires_on,
            "expired": quote.status == QUOTE_EXPIRED,
            "required_works": required,
            "owner_confirmed_works": sorted(confirmed),
            "owner_pending_works": [w for w in gaps],
            "authorization_gaps": gaps,
            "exclusive_requested": request.exclusive,
            "exclusive_ok": self._exclusive_conflict(request, quote) is None,
            "guarantee_pool": pool_id,
            "guarantee_available_minor": available,
            "guarantee_required_minor": chosen.guarantee_required_minor if chosen else 0,
            "guarantee_ok": (chosen.valid_combo if chosen else True),
            "projection": projection,
            "plan_quotes": [vars(p) for p in plans],
            "allocations": [vars(a) for a in allocations],
            "blockers": blockers,
            "history": quote.history,
        }

    def mutual_exclusion_state(self) -> list[dict]:
        """每个互斥组的生效情况。"""
        groups: dict[str, list[QuoteRecord]] = defaultdict(list)
        for request in self.store.requests.values():
            groups[request.alternative_group].append(self.quotes[request.id])
        out = []
        for group, quotes in sorted(groups.items()):
            formed = [q.request_id for q in quotes if q.status == QUOTE_FORMED]
            out.append({
                "alternative_group": group,
                "formed": formed,
                "active_pending": [q.request_id for q in quotes if q.status == QUOTE_PENDING],
                "consistent": len(formed) <= 1,
            })
        return out
