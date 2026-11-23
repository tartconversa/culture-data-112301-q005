"""应用服务门面：把资料、引擎与本地状态装配为可重复调用的业务能力。"""
from __future__ import annotations

from pathlib import Path

from .amendments import AmendmentService
from .evidence import ConsumptionEngine
from .lineage import Lineage
from .pricing import RenewalAnalyzer
from .quoting import QuoteManager
from .statements_reconcile import StatementLedger, build_ledger, observed_by_dimension, reconcile
from .store import MaterialStore, StateStore
from . import models


class Application:
    def __init__(self, root: Path | str | None = None, as_of: str | None = None):
        self.store = MaterialStore(Path(root) if root else None) if root else MaterialStore()
        self.as_of = as_of
        self.lineage = Lineage(self.store.works, self.store.late_evidence)
        self.engine = ConsumptionEngine(self.store, self.lineage, as_of=as_of)
        self.analyzer = RenewalAnalyzer(self.store, self.lineage, self.engine)
        self.quotes = QuoteManager(self.store, self.lineage, self.analyzer)
        self.amendments = AmendmentService(self.store, self.lineage, self.quotes)
        self.state = StateStore()
        self.quotes.attach_sink(self.state)
        self.ledger = build_ledger(self.store)
        self._replay()

    def _replay(self) -> None:
        entries = self.state.read_ledger()
        quote_entries = [e for e in entries if e.get("domain") == "quote"]
        self.quotes.replay(quote_entries)
        for entry in entries:
            if entry.get("domain") == "statement_import":
                self.ledger.import_file(entry["path"])
            if entry.get("domain") == "scope_narrowing":
                self.amendments.register_narrowing(
                    entry["id"], entry["release"], entry["on"],
                    tuple(entry.get("dropped_territories", [])),
                    tuple(entry.get("dropped_works", [])),
                )

    # ------------------------------------------------------------------
    # 用量与权利查询
    # ------------------------------------------------------------------
    def consumption_lines(self) -> list[dict]:
        return [models.to_dict(line) for line in self.engine.all_lines()]

    def batches(self) -> list[dict]:
        return [models.to_dict(b) for b in self.engine.batches()]

    def usage_disputes(self) -> list[dict]:
        """仅返回存在冲突、证据不足或未授权的使用。"""
        out = []
        for line in self.engine.all_lines():
            if line.conflicts or not line.billable:
                out.append(models.to_dict(line))
        return out

    def rights_coverage(self) -> list[dict]:
        rows = []
        for line in self.engine.all_lines():
            for right in line.rights:
                rows.append({
                    "origin_event": line.origin_event,
                    "release": line.release,
                    "customer": line.customer,
                    "territory": line.territory,
                    "on": line.on,
                    "stage": line.stage,
                    "work": right.work,
                    "owner_at_use": right.owner_at_use,
                    "covered": right.covered,
                    "license_ids": right.license_ids,
                    "reasons": right.reasons,
                })
        return rows

    def guarantee_usage(self) -> list[dict]:
        reservations = self.quotes._guarantee_reservations()
        out = []
        for pool in self.store.guarantees.values():
            extra = reservations.get(pool.id, 0)
            out.append({
                "pool": pool.id,
                "currency": pool.currency,
                "limit_minor": pool.limit_minor,
                "baseline_reserved_minor": pool.reserved_minor,
                "quote_reserved_minor": extra,
                "available_minor": pool.limit_minor - pool.reserved_minor - extra,
                "works": list(pool.works),
                "territories": list(pool.territories),
            })
        return out

    # ------------------------------------------------------------------
    # 声明
    # ------------------------------------------------------------------
    def import_statements(self, path: str) -> int:
        added = self.ledger.import_file(path)
        self.state.append({"domain": "statement_import", "path": str(path)})
        return added

    def reconciliation(self) -> list[dict]:
        lines = self.engine.all_lines()
        # 发行包 -> 触及作品（含原作）
        work_map = {}
        for rid, resolved in self.engine.resolved_releases.items():
            works: list[str] = []
            for work_id in resolved.release.works:
                for wid in self.lineage.effective_works(work_id):
                    if wid not in works:
                        works.append(wid)
            work_map[rid] = works
        observed = observed_by_dimension(lines, work_map)
        return [models.to_dict(r) for r in reconcile(self.ledger, observed)]

    # ------------------------------------------------------------------
    # 续约与报价
    # ------------------------------------------------------------------
    def renewal_summary(self) -> list[dict]:
        return self.analyzer.customer_usage_summary()

    def evaluate_request(self, request_id: str) -> dict:
        return self.quotes.evaluate(request_id)

    def issue_quote(self, request_id: str, plan_id: str, on: str) -> dict:
        self.quotes.issue(request_id, plan_id, on)
        self.state.save_snapshot({"quotes": {rid: q.to_dict() for rid, q in self.quotes.quotes.items()}})
        return self.quotes.evaluate(request_id)

    def expire_quote(self, request_id: str, on: str) -> dict:
        self.quotes.expire(request_id, on)
        self.state.save_snapshot({"quotes": {rid: q.to_dict() for rid, q in self.quotes.quotes.items()}})
        return self.quotes.evaluate(request_id)

    def withdraw(self, request_id: str, on: str) -> dict:
        self.quotes.withdraw(request_id, on)
        self.state.save_snapshot({"quotes": {rid: q.to_dict() for rid, q in self.quotes.quotes.items()}})
        return self.quotes.evaluate(request_id)

    def mutual_exclusion(self) -> list[dict]:
        return self.quotes.mutual_exclusion_state()

    # ------------------------------------------------------------------
    # 修正影响
    # ------------------------------------------------------------------
    def amendment_impacts(self) -> list[dict]:
        return [models.to_dict(impact) for impact in self.amendments.all_impacts(self.ledger)]

    def register_narrowing(self, narrowing_id: str, release: str, on: str,
                           dropped_territories=(), dropped_works=()) -> dict:
        narrowing = self.amendments.register_narrowing(
            narrowing_id, release, on, tuple(dropped_territories), tuple(dropped_works)
        )
        self.state.append({
            "domain": "scope_narrowing",
            "id": narrowing.id,
            "release": narrowing.release,
            "on": narrowing.on,
            "dropped_territories": list(narrowing.dropped_territories),
            "dropped_works": list(narrowing.dropped_works),
        })
        return vars(narrowing)
