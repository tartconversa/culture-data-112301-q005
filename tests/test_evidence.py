"""证据效力、镜像去重、衍生谱系与许可消耗规则测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.evidence import ConsumptionEngine
from app.lineage import Lineage
from app.models import (
    EVIDENCE_PACKAGE,
    EVIDENCE_PREVIEW,
    EVIDENCE_RELEASE_RECEIPT,
)
from tests.fixtures import event, license_, make_store, release, work


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.works = [
            work("W0", "H0"),
            work("W1", "H1"),
            work("W8", "H0", parents=("W0", "W1")),
            work("W9", "H1", parents=("W8",)),
        ]

    def test_ancestors_are_transitive_and_deduped(self):
        lineage = Lineage({w.id: w for w in self.works}, [])
        self.assertEqual(lineage.ancestors("W9"), ("W8", "W0", "W1"))
        self.assertEqual(lineage.effective_works("W0"), ("W0",))
        self.assertTrue(lineage.is_derivative("W8"))

    def test_ownership_ruling_only_effective_from_ruling_date(self):
        from app.models import LateEvidence

        ruling = LateEvidence("LE1", "ownership_ruling", None, "W8", "2026-10-12", "H2")
        lineage = Lineage({w.id: w for w in self.works}, [ruling])
        self.assertEqual(lineage.owner_on("W8", "2026-10-11"), "H0")
        self.assertEqual(lineage.owner_on("W8", "2026-10-12"), "H2")
        self.assertEqual(lineage.owner_on("W8", "2027-01-01"), "H2")
        # 未裁决作品不受影响
        self.assertEqual(lineage.owner_on("W0", "2027-01-01"), "H0")


class ConsumptionTests(unittest.TestCase):
    def _engine(self, store, rulings=None):
        lineage = Lineage(store.works, rulings or [])
        return ConsumptionEngine(store, lineage), lineage

    def test_mirror_events_are_counted_once(self):
        store = make_store(
            works=[work("W0", "H0")],
            licenses=[license_("L0", "W0", "C0", ("EU",))],
            releases=[release("R0", works=("W0",), released="2026-02-10", receipt="2026-02-12", end="2026-03-01")],
            events=[event("EV1", "R0", units=10)],
        )
        engine, _ = self._engine(store)
        lines = engine.all_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].units, 10)
        self.assertEqual(lines[0].evidence_strength, EVIDENCE_RELEASE_RECEIPT)
        self.assertTrue(lines[0].billable)

    def test_preview_never_billable_even_with_license(self):
        store = make_store(
            works=[work("W0", "H0")],
            licenses=[license_("L0", "W0", "C0", ("EU",), purpose="preview")],
            releases=[release("R0", works=("W0",))],
            events=[event("EV1", "R0", stage="preview", units=10)],
        )
        engine, _ = self._engine(store)
        line = engine.all_lines()[0]
        self.assertEqual(line.evidence_strength, EVIDENCE_PREVIEW)
        self.assertFalse(line.billable)
        self.assertIn("preview_does_not_grant_release_right", line.conflicts)

    def test_preview_only_license_does_not_cover_release_usage(self):
        store = make_store(
            works=[work("W0", "H0")],
            licenses=[license_("L0", "W0", "C0", ("EU",), purpose="preview")],
            releases=[release("R0", works=("W0",), released="2026-02-10", receipt="2026-02-12")],
            events=[event("EV1", "R0", stage="release", units=10)],
        )
        engine, _ = self._engine(store)
        line = engine.all_lines()[0]
        self.assertFalse(line.billable)
        self.assertFalse(line.rights[0].covered)
        self.assertIn("preview_only_license_no_release_right", line.rights[0].reasons)

    def test_derivative_usage_requires_all_ancestor_rights(self):
        store = make_store(
            works=[work("W0", "H0"), work("W1", "H1"), work("W8", "H0", parents=("W0", "W1"))],
            # 只有衍生素材自身有许可，原作 W1 无许可
            licenses=[license_("L8", "W8", "C0", ("EU",))],
            releases=[release("R0", works=("W8",), released="2026-02-10", receipt="2026-02-12")],
            events=[event("EV1", "R0", stage="release", units=5)],
        )
        engine, _ = self._engine(store)
        line = engine.all_lines()[0]
        self.assertEqual(set(line.effective_works), {"W8", "W0", "W1"})
        self.assertFalse(line.all_rights_covered)
        self.assertFalse(line.billable)
        by_work = {r.work: r.covered for r in line.rights}
        self.assertTrue(by_work["W8"])
        self.assertFalse(by_work["W0"])
        self.assertFalse(by_work["W1"])

    def test_territory_mismatch_is_unlicensed(self):
        store = make_store(
            works=[work("W0", "H0")],
            licenses=[license_("L0", "W0", "C0", ("NA",))],
            releases=[release("R0", territory="EU", works=("W0",),
                              released="2026-02-10", receipt="2026-02-12")],
            events=[event("EV1", "R0", stage="release")],
        )
        engine, _ = self._engine(store)
        line = engine.all_lines()[0]
        self.assertFalse(line.billable)
        self.assertTrue(any("territory_not_licensed" in r for r in line.rights[0].reasons))

    def test_missing_receipt_is_still_usage_but_package_strength(self):
        store = make_store(
            works=[work("W0", "H0")],
            licenses=[license_("L0", "W0", "C0", ("EU",))],
            releases=[release("R0", works=("W0",), released=None, receipt=None)],
            events=[event("EV1", "R0", stage="release")],
        )
        engine, _ = self._engine(store)
        line = engine.all_lines()[0]
        self.assertEqual(line.evidence_strength, EVIDENCE_PACKAGE)
        self.assertIn("no_receipt_late_channel_reporting_possible", line.conflicts)
        # 有授权的打包/发行阶段使用仍可计收，不因为缺回执归零
        self.assertTrue(line.billable)

    def test_late_receipt_evidence_upgrades_release(self):
        from app.models import LateEvidence

        late = LateEvidence("LE1", "release_receipt", "R0", None, "2026-10-10", "2026-04-01")
        store = make_store(
            works=[work("W0", "H0")],
            licenses=[license_("L0", "W0", "C0", ("EU",))],
            releases=[release("R0", works=("W0",), released=None, receipt=None, end="2026-05-01")],
            events=[event("EV1", "R0", stage="release")],
            late_evidence=[late],
        )
        engine, _ = self._engine(store)
        resolved = engine.resolved_releases["R0"]
        self.assertEqual(resolved.effective_released_on, "2026-04-01")
        self.assertEqual(resolved.effective_receipt_on, "2026-10-10")
        self.assertEqual(engine.all_lines()[0].evidence_strength, EVIDENCE_RELEASE_RECEIPT)

    def test_coverage_dispute_blocks_billing_conservatively(self):
        store = make_store(
            works=[work("W0", "H0")],
            licenses=[license_("L0", "W0", "C0", ("EU",))],
            releases=[release("R0", works=("W0",), released="2026-02-10", receipt="2026-02-12")],
            events=[event("EV1", "R0", stage="release", coverage=0.6, disputed=True)],
        )
        engine, _ = self._engine(store)
        line = engine.all_lines()[0]
        self.assertTrue(line.coverage_disputed)
        self.assertIn("coverage_conflict_logs_disagree", line.conflicts)
        # 保守口径：覆盖存疑不直接计正常收入
        self.assertFalse(line.billable)
        observed, high = engine.estimated_units(store.events["EV1"])
        self.assertEqual(observed, 10)
        self.assertAlmostEqual(high, 10 / 0.6)

    def test_complete_vs_observing_batches(self):
        store = make_store(
            works=[work("W0", "H0")],
            licenses=[license_("L0", "W0", "C0", ("EU",))],
            releases=[
                release("Rdone", works=("W0",), released="2026-02-10", receipt="2026-02-12", end="2026-03-01"),
                release("Ropen", works=("W0",), released="2026-02-10", receipt="2026-02-12", end=None),
            ],
            events=[
                event("EV1", "Rdone", stage="release"),
                event("EV2", "Ropen", stage="release", coverage=0.6, disputed=True),
            ],
        )
        engine, _ = self._engine(store)
        statuses = {b.release: b for b in engine.batches()}
        self.assertEqual(statuses["Rdone"].status, "complete")
        self.assertEqual(statuses["Ropen"].status, "observing")
        self.assertFalse(statuses["Ropen"].coverage_complete)
        self.assertIn("release_period_not_ended", statuses["Ropen"].reasons)

    def test_exclusive_license_conflict_flagged(self):
        store = make_store(
            works=[work("W0", "H0")],
            licenses=[
                license_("L0", "W0", "C1", ("EU",), exclusive=True),
                license_("L1", "W0", "C0", ("EU",), exclusive=False),
            ],
            releases=[release("R0", customer="C0", works=("W0",),
                              released="2026-02-10", receipt="2026-02-12")],
            events=[event("EV1", "R0", stage="release")],
        )
        engine, _ = self._engine(store)
        line = engine.all_lines()[0]
        self.assertFalse(line.billable)
        self.assertTrue(any("exclusive_license_held_by_C1" in c for c in line.conflicts))


if __name__ == "__main__":
    unittest.main()
