"""全量合成资料上的端到端口径测试。

这些测试不假设具体业务数值，只验证需求中的不变量在真实资料规模下成立。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.service import Application
from app.store import DataIntegrityError


class ApplicationEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Application()

    def test_all_mirror_logs_deduped_to_origin_events(self):
        lines = self.app.consumption_lines()
        # 1800 条镜像日志 -> 900 个 origin_event
        self.assertEqual(len(lines), len(self.app.store.events))
        origins = [l["origin_event"] for l in lines]
        self.assertEqual(len(origins), len(set(origins)))

    def test_every_line_explains_scope_rights_and_conflicts(self):
        for line in self.app.consumption_lines():
            self.assertTrue(line["effective_works"])
            self.assertTrue(line["rights"])
            for right in line["rights"]:
                self.assertIn("covered", right)
                self.assertTrue(right["reasons"] is not None)
            self.assertIn("evidence_strength", line)

    def test_unlicensed_usage_never_billable(self):
        for line in self.app.consumption_lines():
            if not line["billable"]:
                self.assertTrue(line["conflicts"] or line["stage"] == "preview")
            if line["billable"]:
                self.assertTrue(all(r["covered"] for r in line["rights"]))
                self.assertNotIn("preview", line["stage"])

    def test_preview_lines_never_billable(self):
        self.assertFalse(any(l["billable"] for l in self.app.consumption_lines() if l["stage"] == "preview"))

    def test_complete_batches_require_ended_period_and_full_coverage(self):
        for batch in self.app.batches():
            if batch["status"] == "complete":
                self.assertIsNotNone(batch["end_on"])
                self.assertTrue(batch["coverage_complete"])
            else:
                self.assertTrue(batch["reasons"])

    def test_late_receipt_rescued_r000(self):
        r000 = next(b for b in self.app.batches() if b["release"] == "R000")
        self.assertEqual(r000["receipt_on"], "2026-10-10")
        self.assertEqual(r000["released_on"], "2026-04-01")

    def test_ownership_ruling_reflected_for_future_quotes(self):
        # W8 在 2026-10-12 后权利人变为 H2
        self.assertEqual(self.app.lineage.owner_on("W8", "2026-10-11"), "H0")
        self.assertEqual(self.app.lineage.owner_on("W8", "2027-01-01"), "H2")
        impacts = self.app.amendment_impacts()
        rulings = [i for i in impacts if i["kind"] == "ownership_ruling"]
        self.assertTrue(rulings)
        self.assertIn("L8", rulings[0]["affected_licenses"])

    def test_plan_quotes_only_use_legal_combos(self):
        legal = {("usage",), ("capped",), ("guarantee",), ("guarantee", "usage")}
        for rid in self.app.store.requests:
            evaluation = self.app.evaluate_request(rid)
            for quote in evaluation["plan_quotes"]:
                self.assertIn(tuple(quote["term_ids"]), legal)

    def test_q0_stays_pending_on_real_data(self):
        evaluation = self.app.evaluate_request("Q0")
        self.assertEqual(evaluation["status"], "draft")  # 未签发前为 draft
        self.app.quotes.issue("Q0", "usage", "2026-10-06", record=False)
        evaluation = self.app.evaluate_request("Q0")
        self.assertEqual(evaluation["status"], "pending")
        self.assertIn("W8", evaluation["authorization_gaps"])

    def test_reconciliation_uses_one_row_per_dimension(self):
        rows = self.app.reconciliation()
        keys = [(r["customer"], r["quarter"], r["territory"], r["work"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)))

    def test_guarantee_pools_report_availability(self):
        pools = self.app.guarantee_usage()
        self.assertEqual(len(pools), 3)
        for pool in pools:
            self.assertGreaterEqual(pool["available_minor"], 0)
            self.assertEqual(
                pool["available_minor"],
                pool["limit_minor"] - pool["baseline_reserved_minor"] - pool["quote_reserved_minor"],
            )

    def test_import_and_revision_flow_with_state_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            extra = Path(directory) / "extra.jsonl"
            extra.write_text(
                '{"id":"SX1","customer":"C0","quarter":"2026Q1","territory":"EU",'
                '"work":"W0","units":21,"revision":2,"received_on":"2026-11-02"}\n',
                encoding="utf-8",
            )
            added = self.app.import_statements(extra)
            self.assertEqual(added, 1)
            # 重新构造应用：账本重放后修订仍然生效
            replayed = Application()
            row = next(
                r for r in replayed.reconciliation()
                if (r["customer"], r["work"]) == ("C0", "W0")
            )
            self.assertEqual(row["latest_revision"], 2)
            self.assertIn("S0", row["superseded_statement_ids"])
            replayed.state.reset()

    def test_corrupted_data_is_rejected(self):
        # 应用复用资料指纹校验：损坏目录必须抛出 DataIntegrityError
        import shutil

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copytree(Path(__file__).resolve().parents[1] / "data", target / "data")
            with (target / "data" / "works.jsonl").open("a", encoding="utf-8") as stream:
                stream.write('{"id":"WX","kind":"music","parents":[],"owner":"H9"}\n')
            with self.assertRaises(DataIntegrityError):
                Application(root=target)

    def test_usage_reduction_is_explained_not_treated_as_elasticity(self):
        # 汇总同时给出口径用量与日志覆盖率，业务侧不能把覆盖下降误当需求下降
        for row in self.app.renewal_summary():
            self.assertIn("log_coverage_ratio", row)
            self.assertIn("observing_batch_units", row)
            self.assertGreaterEqual(row["observed_units"], row["billable_units"])


if __name__ == "__main__":
    unittest.main()
