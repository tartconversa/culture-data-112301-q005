"""报价生命周期、授权次序、互斥组与修正影响测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.amendments import AmendmentService
from app.evidence import ConsumptionEngine
from app.lineage import Lineage
from app.models import LateEvidence
from app.pricing import RenewalAnalyzer
from app.quoting import (
    QUOTE_EXPIRED,
    QUOTE_FORMED,
    QUOTE_PENDING,
    QUOTE_WITHDRAWN,
    QuoteManager,
)
from tests.fixtures import auth, event, license_, make_store, pool, release, request_, work


def _manager(store):
    lineage = Lineage(store.works, store.late_evidence)
    engine = ConsumptionEngine(store, lineage)
    analyzer = RenewalAnalyzer(store, lineage, engine)
    return QuoteManager(store, lineage, analyzer), lineage, analyzer, engine


def base_store(**overrides):
    kwargs = dict(
        works=[work("W0", "H0"), work("W1", "H1"), work("W8", "H0", parents=("W0", "W1"))],
        licenses=[
            license_("L0", "W0", "C0", ("EU",), pool="GP0"),
            license_("L1", "W1", "C0", ("EU",), pool="GP0"),
            license_("L8", "W8", "C0", ("EU",), pool="GP0"),
        ],
        releases=[release("R0", works=("W0", "W8"), released="2026-02-10", receipt="2026-02-12")],
        events=[event("EV1", "R0", units=50)],
        guarantees=[pool("GP0", limit=500000, reserved=100000, works=("W0", "W1", "W8"))],
        requests=[
            request_("Q0", works=("W0",), group="ALT0", expires="2026-11-15"),
            request_("Q6", works=("W1",), group="ALT0", expires="2026-11-15"),
        ],
    )
    kwargs.update(overrides)
    return make_store(**kwargs)


class LifecycleTests(unittest.TestCase):
    def test_pending_until_both_customer_and_owner_act(self):
        store = base_store(auth_events=[
            auth("AE1", "Q0", "C0", "customer_accept", "2026-10-04"),
        ])
        manager, _, _, _ = _manager(store)
        manager.issue("Q0", "usage", "2026-10-01", record=False)
        self.assertEqual(manager.quotes["Q0"].status, QUOTE_PENDING)

    def test_forms_when_all_confirmations_present_regardless_of_order(self):
        # 权利人确认先于客户接受
        store = base_store(auth_events=[
            auth("AE2", "Q0", "H0", "owner_confirm", "2026-10-02", work_id="W0"),
            auth("AE1", "Q0", "C0", "customer_accept", "2026-10-05"),
        ])
        manager, _, _, _ = _manager(store)
        manager.issue("Q0", "usage", "2026-10-01", record=False)
        quote = manager.quotes["Q0"]
        self.assertEqual(quote.status, QUOTE_FORMED)
        self.assertEqual(quote.formed_on, "2026-10-05")
        self.assertEqual(quote.effective_license_id, "EL-Q0")

    def test_derivative_requires_ancestor_owner_confirmation(self):
        store = base_store(auth_events=[
            auth("AE1", "Q0", "C0", "customer_accept", "2026-10-04"),
            auth("AE2", "Q0", "H0", "owner_confirm", "2026-10-05", work_id="W8"),
            # W8 触及 W0/W1，二者均无确认
        ])
        store.requests["Q0"] = request_("Q0", works=("W8",))
        manager, _, _, _ = _manager(store)
        manager.issue("Q0", "usage", "2026-10-06", record=False)
        self.assertEqual(manager.quotes["Q0"].status, QUOTE_PENDING)
        evaluation = manager.evaluate("Q0")
        self.assertIn("W0", evaluation["authorization_gaps"])
        self.assertIn("W1", evaluation["authorization_gaps"])

    def test_expired_quote_does_not_form_and_releases_capacity(self):
        store = base_store(auth_events=[
            auth("AE1", "Q0", "C0", "customer_accept", "2026-11-20"),  # 晚于到期日
            auth("AE2", "Q0", "H0", "owner_confirm", "2026-11-20", work_id="W0"),
        ])
        manager, _, _, _ = _manager(store)
        quote = manager.issue("Q0", "guarantee+usage", "2026-11-01", record=False)
        self.assertEqual(quote.status, QUOTE_PENDING)
        manager.expire("Q0", "2026-11-16", record=False)
        self.assertEqual(manager.quotes["Q0"].status, QUOTE_EXPIRED)
        # 过期报价不占用担保
        self.assertEqual(manager._guarantee_reservations(), {})
        # 过期后到达的事件不复活
        manager._apply_authorization_events(manager.quotes["Q0"], store.requests["Q0"])
        self.assertEqual(manager.quotes["Q0"].status, QUOTE_EXPIRED)

    def test_late_issue_after_expiry_becomes_expired(self):
        store = base_store()
        manager, _, _, _ = _manager(store)
        quote = manager.issue("Q0", "usage", "2026-11-20", record=False)
        self.assertEqual(quote.status, QUOTE_EXPIRED)

    def test_withdraw_blocked_after_formation(self):
        store = base_store(auth_events=[
            auth("AE1", "Q0", "C0", "customer_accept", "2026-10-04"),
            auth("AE2", "Q0", "H0", "owner_confirm", "2026-10-05", work_id="W0"),
        ])
        manager, _, _, _ = _manager(store)
        manager.issue("Q0", "usage", "2026-10-06", record=False)
        with self.assertRaisesRegex(ValueError, "已形成有效许可"):
            manager.withdraw("Q0", "2026-10-07", record=False)

    def test_withdraw_pending_quote(self):
        store = base_store()
        manager, _, _, _ = _manager(store)
        manager.issue("Q0", "usage", "2026-10-06", record=False)
        manager.withdraw("Q0", "2026-10-07", record=False)
        self.assertEqual(manager.quotes["Q0"].status, QUOTE_WITHDRAWN)

    def test_mutual_exclusion_only_one_formed_per_group(self):
        store = base_store(auth_events=[
            auth("AE1", "Q0", "C0", "customer_accept", "2026-10-04"),
            auth("AE2", "Q0", "H0", "owner_confirm", "2026-10-05", work_id="W0"),
            auth("AE3", "Q6", "C0", "customer_accept", "2026-10-04"),
            auth("AE4", "Q6", "H1", "owner_confirm", "2026-10-05", work_id="W1"),
        ])
        manager, _, _, _ = _manager(store)
        manager.issue("Q0", "usage", "2026-10-06", record=False)
        manager.issue("Q6", "usage", "2026-10-06", record=False)
        state = {g["alternative_group"]: g for g in manager.mutual_exclusion_state()}
        self.assertEqual(len(state["ALT0"]["formed"]), 1)
        # 第二个报价形成时被同组已生效报价阻塞
        self.assertEqual(manager.quotes["Q6"].status, QUOTE_PENDING)
        self.assertTrue(any("alternative_group_formed_quote" in b for b in manager.evaluate("Q6")["blockers"]))

    def test_formed_commitment_not_replaced_by_later_better_plan(self):
        store = base_store(auth_events=[
            auth("AE1", "Q0", "C0", "customer_accept", "2026-10-04"),
            auth("AE2", "Q0", "H0", "owner_confirm", "2026-10-05", work_id="W0"),
        ])
        manager, _, _, _ = _manager(store)
        manager.issue("Q0", "usage", "2026-10-06", record=False)
        first_license = manager.quotes["Q0"].effective_license_id
        with self.assertRaisesRegex(ValueError, "不能再次签发"):
            manager.issue("Q0", "capped", "2026-10-08", record=False)
        self.assertEqual(manager.quotes["Q0"].effective_license_id, first_license)

    def test_owner_confirmation_must_match_current_owner_after_ruling(self):
        ruling = LateEvidence("LE2", "ownership_ruling", None, "W0", "2026-10-12", "H2")
        store = base_store(late_evidence=[ruling], auth_events=[
            auth("AE1", "Q0", "C0", "customer_accept", "2026-10-13"),
            auth("AE2", "Q0", "H0", "owner_confirm", "2026-10-13", work_id="W0"),  # 旧权利人
        ])
        manager, lineage, _, _ = _manager(store)
        self.assertEqual(lineage.owner_on("W0", "2026-10-13"), "H2")
        manager.issue("Q0", "usage", "2026-10-13", record=False)
        self.assertEqual(manager.quotes["Q0"].status, QUOTE_PENDING)
        self.assertIn("W0", manager.evaluate("Q0")["authorization_gaps"])

    def test_exclusive_request_blocked_by_existing_exclusive_license(self):
        store = base_store(licenses=[
            license_("L0", "W0", "C1", ("EU",), exclusive=True, frm="2026-01-01", to="2027-12-31"),
        ])
        store.requests["Q0"] = request_("Q0", works=("W0",), exclusive=True)
        manager, _, _, _ = _manager(store)
        manager.issue("Q0", "usage", "2026-10-06", record=False)
        self.assertTrue(any("existing_exclusive_license" in b for b in manager.evaluate("Q0")["blockers"]))

    def test_guarantee_reservation_held_by_pending_then_released_on_expiry(self):
        store = base_store()
        manager, _, _, _ = _manager(store)
        manager.issue("Q0", "guarantee+usage", "2026-10-06", record=False)
        self.assertEqual(manager._guarantee_reservations().get("GP0"), 100000)
        manager.expire("Q0", "2026-11-16", record=False)
        self.assertEqual(manager._guarantee_reservations(), {})


class AmendmentTests(unittest.TestCase):
    def test_ruling_keeps_pre_ruling_contract_but_flags_future_usage(self):
        ruling = LateEvidence("LE2", "ownership_ruling", None, "W0", "2026-10-12", "H2")
        store = base_store(late_evidence=[ruling])
        manager, lineage, _, _ = _manager(store)
        service = AmendmentService(store, lineage, manager)
        impacts = service.ownership_ruling_impacts()
        self.assertEqual(len(impacts), 1)
        actions = " ".join(impacts[0].contract_actions)
        self.assertIn("valid_until_2026-10-12_under_previous_owner", actions)
        self.assertIn("supplemental_authorization_or_compensation", actions)
        self.assertIn("L0", impacts[0].affected_licenses)

    def test_scope_narrowing_marks_licenses_and_excludes_future_billing(self):
        store = base_store()
        lineage = Lineage(store.works, [])
        engine = ConsumptionEngine(store, lineage)
        analyzer = RenewalAnalyzer(store, lineage, engine)
        manager = QuoteManager(store, lineage, analyzer)
        service = AmendmentService(store, lineage, manager)
        before = engine.all_lines()[0]
        self.assertTrue(before.billable)
        service.register_narrowing("N1", "R0", "2026-01-01", dropped_territories=("EU",), dropped_works=("W0",))
        impacts = service.narrowing_impacts()
        self.assertEqual(impacts[0].kind, "scope_narrowing")
        self.assertIn("L0", impacts[0].affected_licenses)
        self.assertTrue(any("no_new_charges_in_narrowed_scope" in a for a in impacts[0].contract_actions))
        # 引擎同步：撤回日后的使用不再计正常收入并带冲突说明
        after = engine.all_lines()[0]
        self.assertFalse(after.billable)
        self.assertTrue(any("scope_narrowed_on_2026-01-01" in c for c in after.conflicts))
        self.assertNotIn("W0", after.effective_works)

    def test_ledger_replay_rebuilds_statuses(self):
        store = base_store(auth_events=[
            auth("AE1", "Q0", "C0", "customer_accept", "2026-10-04"),
            auth("AE2", "Q0", "H0", "owner_confirm", "2026-10-05", work_id="W0"),
        ])
        manager, _, _, _ = _manager(store)
        ledger = [
            {"domain": "quote", "action": "issue", "request_id": "Q0", "plan_id": "usage", "on": "2026-10-06"},
        ]
        manager.replay(ledger)
        self.assertEqual(manager.quotes["Q0"].status, QUOTE_FORMED)


if __name__ == "__main__":
    unittest.main()
