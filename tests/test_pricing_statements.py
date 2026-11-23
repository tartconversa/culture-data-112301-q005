"""条款组合、计价、担保容量与声明对账测试。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evidence import ConsumptionEngine
from app.lineage import Lineage
from app.pricing import RenewalAnalyzer, legal_combos, price_combo
from app.statements_reconcile import (
    StatementLedger,
    observed_by_dimension,
    quarter_of,
    reconcile,
)
from tests.fixtures import (
    DEFAULT_TERMS,
    event,
    license_,
    make_store,
    pool,
    release,
    request_,
    statement,
    work,
)


class PricingRuleTests(unittest.TestCase):
    def setUp(self):
        self.terms = {t.id: t for t in DEFAULT_TERMS}

    def test_only_documented_combos_are_legal(self):
        combos = {tuple(c) for c in legal_combos(self.terms)}
        self.assertEqual(
            combos,
            {("usage",), ("capped",), ("guarantee",), ("guarantee", "usage")},
        )

    def test_usage_plan_price(self):
        price = price_combo(self.terms, ("usage",), 1000)
        self.assertEqual(price["total_minor"], 40000)
        self.assertEqual(price["minimum_minor"], 0)

    def test_capped_plan_clamps_between_minimum_and_cap(self):
        low = price_combo(self.terms, ("capped",), 100)
        self.assertEqual(low["total_minor"], 80000)            # 低于最低，取最低
        high = price_combo(self.terms, ("capped",), 100000)
        self.assertEqual(high["total_minor"], 150000)          # 超出封顶
        mid = price_combo(self.terms, ("capped",), 4000)
        self.assertEqual(mid["total_minor"], 100000)           # 25 * 4000

    def test_guarantee_offsets_usage_never_double_counts(self):
        price = price_combo(self.terms, ("guarantee", "usage"), 1000)
        # 按量 40000 低于保底 100000：只收保底，不累加
        self.assertEqual(price["total_minor"], 100000)
        price2 = price_combo(self.terms, ("guarantee", "usage"), 10000)
        # 按量 400000 高于保底：保底被抵扣，只收按量
        self.assertEqual(price2["total_minor"], 400000)
        self.assertEqual(price2["guarantee_required_minor"], 100000)

    def test_cannot_cherry_pick_illegal_combo(self):
        with self.assertRaises(ValueError):
            price_combo(self.terms, ("capped", "usage"), 100)


def _analyzer(store):
    lineage = Lineage(store.works, store.late_evidence)
    engine = ConsumptionEngine(store, lineage)
    return RenewalAnalyzer(store, lineage, engine), lineage, engine


class GuaranteeTests(unittest.TestCase):
    def _store(self, pool_works=("W0",), pool_territories=("EU",), limit=150000, reserved=100000):
        return make_store(
            works=[work("W0", "H0")],
            licenses=[license_("L0", "W0", "C0", ("EU",), pool="GP0")],
            releases=[release("R0", works=("W0",), released="2026-02-10", receipt="2026-02-12")],
            events=[event("EV1", "R0", units=100)],
            guarantees=[pool("GP0", limit=limit, reserved=reserved,
                             works=pool_works, territories=pool_territories)],
            requests=[request_("Q0")],
        )

    def test_guarantee_capacity_blocker_when_insufficient(self):
        analyzer, _, _ = _analyzer(self._store(limit=150000, reserved=100000))
        quotes = analyzer.plan_quotes(request_("Q0"))
        guarantee = next(q for q in quotes if q.plan_id == "guarantee+usage")
        self.assertFalse(guarantee.valid_combo)
        self.assertTrue(any("pool_capacity_shortfall" in b for b in guarantee.blockers))

    def test_guarantee_blocker_when_work_or_territory_excluded(self):
        analyzer, _, _ = _analyzer(self._store(pool_works=("W9",)))
        quotes = analyzer.plan_quotes(request_("Q0"))
        guarantee = next(q for q in quotes if q.plan_id == "guarantee")
        self.assertFalse(guarantee.valid_combo)
        self.assertTrue(any("pool_excludes_works" in b for b in guarantee.blockers))

        analyzer2, _, _ = _analyzer(self._store(pool_territories=("NA",)))
        quotes2 = analyzer2.plan_quotes(request_("Q0"))
        guarantee2 = next(q for q in quotes2 if q.plan_id == "guarantee")
        self.assertTrue(any("pool_excludes_territory" in b for b in guarantee2.blockers))

    def test_other_quote_reservation_reduces_capacity(self):
        analyzer, _, _ = _analyzer(self._store(limit=300000, reserved=100000))
        # 另一报价已占 150000 时，剩余 50000 不足以承载 100000 保底
        quotes = analyzer.plan_quotes(request_("Q0"), extra_reserved={"GP0": 150000})
        guarantee = next(q for q in quotes if q.plan_id == "guarantee")
        self.assertFalse(guarantee.valid_combo)

    def test_allocation_ranges_per_owner_with_uncovered_liability(self):
        store = make_store(
            works=[work("W0", "H0"), work("W1", "H1")],
            licenses=[license_("L0", "W0", "C0", ("EU",)), license_("L1", "W1", "C0", ("EU",), pool="GP1")],
            releases=[release("R0", works=("W0", "W1"), released="2026-02-10", receipt="2026-02-12")],
            events=[event("EV1", "R0", units=100)],
            guarantees=[pool("GP0", works=("W0",), limit=1000)],
            requests=[request_("Q0", works=("W0", "W1"))],
        )
        analyzer, _, _ = _analyzer(store)
        proj = analyzer.project(store.requests["Q0"])
        quote = next(q for q in analyzer.plan_quotes(store.requests["Q0"]) if q.plan_id == "guarantee+usage")
        allocations = analyzer.allocations(store.requests["Q0"], quote, proj)
        self.assertEqual({a.owner for a in allocations}, {"H0", "H1"})
        total_high = sum(a.high_minor for a in allocations)
        self.assertEqual(total_high, quote.total_minor)
        # 担保不可用（W1 不在 GP0）：每个权利人都标出无法被担保覆盖的责任
        self.assertTrue(all(a.uncovered_liability_minor >= 0 for a in allocations))
        self.assertTrue(any(a.uncovered_liability_minor > 0 for a in allocations))


class StatementReconcileTests(unittest.TestCase):
    def test_latest_revision_wins_and_superseded_kept(self):
        ledger = StatementLedger([
            statement("S0", "C0", "2026Q1", "EU", "W0", 50, revision=1),
            statement("S1", "C0", "2026Q1", "EU", "W0", 30, revision=2),
        ])
        latest = ledger.latest_by_dimension()
        self.assertEqual(latest[("C0", "2026Q1", "EU", "W0")].units, 30)
        self.assertEqual([s.id for s in ledger.superseded()], ["S0"])

    def test_reconcile_statuses(self):
        ledger = StatementLedger([
            statement("S0", "C0", "2026Q1", "EU", "W0", 100, revision=1),   # 多报
            statement("S1", "C0", "2026Q1", "EU", "W1", 20, revision=1),    # 少报
            statement("S2", "C0", "2026Q1", "EU", "W2", 99, revision=1),    # 无观察
        ])
        observed = {
            ("C0", "2026Q1", "EU", "W0"): 80,
            ("C0", "2026Q1", "EU", "W1"): 40,
        }
        rows = {(r.work,): r for r in reconcile(ledger, observed)}
        self.assertEqual(rows[("W0",)].status, "overstated")
        self.assertEqual(rows[("W1",)].status, "understated")
        self.assertEqual(rows[("W2",)].status, "no_observation")

    def test_preview_events_excluded_from_observed_totals(self):
        store = make_store(
            works=[work("W0", "H0")],
            licenses=[license_("L0", "W0", "C0", ("EU",))],
            releases=[release("R0", works=("W0",))],
            events=[
                event("EVp", "R0", stage="preview", units=99),
                event("EVr", "R0", stage="release", units=10),
            ],
        )
        lineage = Lineage(store.works, [])
        engine = ConsumptionEngine(store, lineage)
        work_map = {"R0": ["W0"]}
        observed = observed_by_dimension(engine.all_lines(), work_map)
        self.assertEqual(observed[("C0", quarter_of("2026-02-01"), "EU", "W0")], 10)

    def test_import_rejects_reused_identity(self):
        ledger = StatementLedger([statement("S0", "C0", "2026Q1", "EU", "W0", 10)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "more.jsonl"
            path.write_text(
                '{"id":"S0","customer":"C0","quarter":"2026Q1","territory":"EU",'
                '"work":"W0","units":11,"revision":2,"received_on":"2026-11-01"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "声明身份重复"):
                ledger.import_file(path)


if __name__ == "__main__":
    unittest.main()
