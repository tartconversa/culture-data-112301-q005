"""续约条款组合、计价与权利人收入分配。

条款（data/terms.jsonl）：

- usage：按用量单价 40 美分，允许与 guarantee 组合；
- capped：包期条款，单价 25、最低 80000、封顶 150000，不允许与任何条款组合；
- guarantee：最低保证金条款，单价 20、最低 100000，允许与 usage 组合。

组合合法性必须双向满足 allowed_with；不能分别挑出各条款的最低价/封顶拼成
资料中不存在的报价。保底用于抵扣同范围后续按量许可费，不与按量款重复累加。
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from .evidence import ConsumptionEngine
from .lineage import Lineage
from .models import (
    OwnerAllocation,
    PlanQuote,
    RenewalRequest,
    Term,
)
from .store import MaterialStore

PLAN_USAGE = "usage"
PLAN_CAPPED = "capped"
PLAN_GUARANTEE = "guarantee"


def legal_combos(terms: dict[str, Term]) -> list[tuple[str, ...]]:
    """枚举资料允许的全部条款组合（单条款 + 合法多条款）。"""
    ids = sorted(terms)
    combos: list[tuple[str, ...]] = [(i,) for i in ids]
    for size in range(2, len(ids) + 1):
        for candidate in combinations(sorted(ids), size):
            if _combo_allowed(terms, candidate):
                combos.append(candidate)
    return combos


def _combo_allowed(terms: dict[str, Term], combo: tuple[str, ...]) -> bool:
    for term_id in combo:
        others = set(combo) - {term_id}
        if not others.issubset(set(terms[term_id].allowed_with)):
            return False
    return True


def price_combo(terms: dict[str, Term], combo: tuple[str, ...], units: int) -> dict:
    """计算一个合法组合的计价构成。"""
    kinds = [terms[t].kind for t in combo]
    usage_charge = 0
    minimum = 0
    cap = None
    guarantee_part = 0
    if kinds == ["usage"]:
        term = terms[PLAN_USAGE]
        usage_charge = term.unit_minor * units
    elif kinds == ["period"]:
        term = terms[PLAN_CAPPED]
        usage_charge = term.unit_minor * units
        minimum = term.minimum_minor
        cap = term.cap_minor
    elif kinds == ["minimum"]:
        term = terms[PLAN_GUARANTEE]
        usage_charge = term.unit_minor * units
        minimum = term.minimum_minor
        guarantee_part = minimum
    elif sorted(kinds) == ["minimum", "usage"]:
        # 保底抵扣同范围按量费：只取两者较大值，绝不重复累加
        usage_term = terms[PLAN_USAGE]
        guarantee_term = terms[PLAN_GUARANTEE]
        usage_charge = usage_term.unit_minor * units
        minimum = guarantee_term.minimum_minor
        guarantee_part = minimum
    else:  # pragma: no cover - legal_combos 已排除
        raise ValueError(f"不支持的条款组合: {combo}")
    total = max(usage_charge, minimum)
    if cap is not None:
        total = min(total, cap)
    return {
        "usage_charge_minor": usage_charge,
        "minimum_minor": minimum,
        "cap_minor": cap,
        "guarantee_required_minor": guarantee_part,
        "total_minor": total,
    }


class RenewalAnalyzer:
    def __init__(self, store: MaterialStore, lineage: Lineage, engine: ConsumptionEngine):
        self.store = store
        self.lineage = lineage
        self.engine = engine
        self.lines = engine.all_lines()
        self.batches = {b.release: b for b in engine.batches()}
        self.combos = legal_combos(store.terms)

    # ------------------------------------------------------------------
    # 用量投影
    # ------------------------------------------------------------------
    def project(self, request: RenewalRequest) -> dict:
        """按客户/地域投影续约期用量，并区分完整/观察批次。

        历史事件按发行包归属；请求作品及其可达原作都纳入范围（release 用途
        必须覆盖原作权利）。
        """
        wanted = set()
        for work_id in request.works:
            wanted.update(self.lineage.effective_works(work_id))
        observed = 0
        observed_high = 0          # 按覆盖率折算的上界
        complete_units = 0
        observing_units = 0
        unlicensed_units = 0
        by_work: dict[str, int] = defaultdict(int)
        by_work_high: dict[str, float] = defaultdict(float)
        batch_ids: set[str] = set()
        for line in self.lines:
            if line.stage == "preview":
                continue
            if line.customer != request.customer or line.territory != request.territory:
                continue
            touched = wanted.intersection(line.effective_works)
            if not touched:
                continue
            batch_ids.add(line.release)
            event = self.store.events[line.origin_event]
            high_units = event.observed_units / event.coverage
            observed += line.units
            observed_high += high_units
            if self.batches[line.release].status == "complete":
                complete_units += line.units
            else:
                observing_units += line.units
            if not line.billable:
                unlicensed_units += line.units
            for work_id in touched:
                by_work[work_id] += line.units
                by_work_high[work_id] += high_units
        return {
            "request_id": request.id,
            "works": sorted(wanted),
            "observed_units": observed,
            "high_estimate_units": int(round(observed_high)),
            "complete_batch_units": complete_units,
            "observing_batch_units": observing_units,
            "unlicensed_units": unlicensed_units,
            "batches": sorted(batch_ids),
            "by_work": dict(sorted(by_work.items())),
            "by_work_high": {k: int(round(v)) for k, v in sorted(by_work_high.items())},
        }

    # ------------------------------------------------------------------
    # 方案报价
    # ------------------------------------------------------------------
    def plan_quotes(self, request: RenewalRequest, extra_reserved: dict[str, int] | None = None) -> list[PlanQuote]:
        projection = self.project(request)
        units = projection["observed_units"]
        quotes: list[PlanQuote] = []
        for combo in self.combos:
            price = price_combo(self.store.terms, combo, units)
            blockers = list(self._guarantee_blockers(request, combo, price, extra_reserved or {}))
            pool_id = self._pool_for(request)
            quotes.append(
                PlanQuote(
                    plan_id="+".join(combo),
                    request_id=request.id,
                    term_ids=list(combo),
                    term_kinds=[self.store.terms[t].kind for t in combo],
                    projected_units=units,
                    usage_charge_minor=price["usage_charge_minor"],
                    minimum_minor=price["minimum_minor"],
                    cap_minor=price["cap_minor"],
                    total_minor=price["total_minor"],
                    guarantee_pool=pool_id if price["guarantee_required_minor"] else None,
                    guarantee_required_minor=price["guarantee_required_minor"],
                    valid_combo=not blockers,
                    blockers=blockers,
                )
            )
        return quotes

    def _pool_for(self, request: RenewalRequest) -> str | None:
        """通过该客户既有许可的 guarantee_pool 找到对应共享担保池。"""
        pools = {
            lic.guarantee_pool
            for lic in self.store.licenses.values()
            if lic.customer == request.customer
        }
        return sorted(pools)[0] if pools else None

    def _guarantee_blockers(
        self,
        request: RenewalRequest,
        combo: tuple[str, ...],
        price: dict,
        extra_reserved: dict[str, int],
    ) -> list[str]:
        required = price["guarantee_required_minor"]
        if not required:
            return []
        pool_id = self._pool_for(request)
        if pool_id is None:
            return ["no_guarantee_pool_available"]
        pool = self.store.guarantees[pool_id]
        blockers = []
        missing_works = sorted(set(request.works) - set(pool.works))
        if missing_works:
            blockers.append(f"pool_excludes_works:{','.join(missing_works)}")
        if request.territory not in pool.territories:
            blockers.append(f"pool_excludes_territory:{request.territory}")
        available = pool.limit_minor - pool.reserved_minor - extra_reserved.get(pool_id, 0)
        if required > available:
            blockers.append(
                f"pool_capacity_shortfall:required={required}>available={available}"
            )
        return blockers

    # ------------------------------------------------------------------
    # 权利人收入区间与担保覆盖
    # ------------------------------------------------------------------
    def allocations(
        self,
        request: RenewalRequest,
        quote: PlanQuote,
        projection: dict,
    ) -> list[OwnerAllocation]:
        owner_works: dict[str, list[str]] = defaultdict(list)
        owner_units: dict[str, int] = defaultdict(int)
        for work_id in projection["works"]:
            owner = self.lineage.owner_on(work_id, request.from_date)
            owner_works[owner].append(work_id)
            owner_units[owner] += projection["by_work"].get(work_id, 0)
        total_units = sum(owner_units.values())
        results: list[OwnerAllocation] = []
        for owner in sorted(owner_works):
            share = (owner_units[owner] / total_units) if total_units else 0.0
            high = int(round(quote.total_minor * share))
            guaranteed = int(round(quote.minimum_minor * share))
            low = min(guaranteed, high)
            uncovered = 0
            reasons: list[str] = []
            if quote.guarantee_required_minor and quote.blockers:
                uncovered = guaranteed
                reasons.append("guarantee_not_covered:" + ";".join(quote.blockers))
            if owner_units[owner] == 0:
                reasons.append("no_historical_observed_units_share_provisional")
            results.append(
                OwnerAllocation(
                    owner=owner,
                    works=sorted(owner_works[owner]),
                    low_minor=low,
                    high_minor=high,
                    guaranteed_minor=guaranteed,
                    uncovered_liability_minor=uncovered,
                    reasons=reasons,
                )
            )
        return results

    # ------------------------------------------------------------------
    # 客户/用途/地域层面的续约分析
    # ------------------------------------------------------------------
    def customer_usage_summary(self) -> list[dict]:
        """汇总各客户/地域的用量与回收不确定性，区分观察状态。"""
        summary: dict[tuple[str, str], dict] = {}
        for line in self.lines:
            if line.stage == "preview":
                continue
            key = (line.customer, line.territory)
            bucket = summary.setdefault(key, {
                "customer": line.customer,
                "territory": line.territory,
                "observed_units": 0,
                "billable_units": 0,
                "unlicensed_units": 0,
                "complete_batch_units": 0,
                "observing_batch_units": 0,
                "coverage_disputed_units": 0,
                "log_coverage_ratio": 0.0,
            })
            bucket["observed_units"] += line.units
            if line.billable:
                bucket["billable_units"] += line.units
            else:
                bucket["unlicensed_units"] += line.units
            batch = self.batches[line.release]
            if batch.status == "complete":
                bucket["complete_batch_units"] += line.units
            else:
                bucket["observing_batch_units"] += line.units
            if line.coverage_disputed:
                bucket["coverage_disputed_units"] += line.units
        for bucket in summary.values():
            observed = bucket["observed_units"] or 1
            bucket["log_coverage_ratio"] = round(
                (observed - bucket["coverage_disputed_units"]) / observed, 4
            )
        return [summary[k] for k in sorted(summary)]
