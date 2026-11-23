"""资料修正对未来报价与既有许可的影响判断。

三类修正：

1. 声明修正（statement revision）：替换同维度旧申报，刷新对账与未来用量
   投影；历史已成立许可的收入按当时申报解释，不追溯改写。
2. 归属裁决（ownership_ruling）：自裁决日起更换权利人。裁决日前成立的许可
   仍按当时权利人解释（继续有效但列入观察），裁决日后的新报价必须取得新权
   利人授权；覆盖裁决日之后使用的旧许可需要补授权或补偿。
3. 发行范围缩小（scope_narrowing，由后续导入登记）：标记发行包撤回地域/
   作品，撤回日之后的使用从 billable 投影中移除；已成立许可在撤回范围内可
   继续持有但不再产生新计费，超出剩余范围的使用需要补授权。

原则：绝不用新权利表覆盖当时有效的合同；每个结论都给出可继续/需补授权/
需补偿的具体许可清单。
"""
from __future__ import annotations

from dataclasses import dataclass

from .lineage import Lineage
from .models import AmendmentImpact, LateEvidence
from .quoting import QuoteManager, QUOTE_FORMED
from .store import MaterialStore


@dataclass
class ScopeNarrowing:
    id: str
    release: str
    on: str
    dropped_territories: tuple[str, ...]
    dropped_works: tuple[str, ...]


class AmendmentService:
    def __init__(self, store: MaterialStore, lineage: Lineage, quotes: QuoteManager):
        self.store = store
        self.lineage = lineage
        self.quotes = quotes
        self.scope_narrowings: list[ScopeNarrowing] = []

    def register_narrowing(
        self,
        narrowing_id: str,
        release: str,
        on: str,
        dropped_territories: tuple[str, ...] = (),
        dropped_works: tuple[str, ...] = (),
    ) -> ScopeNarrowing:
        if release not in self.store.releases:
            raise ValueError(f"未知发行包: {release}")
        narrowing = ScopeNarrowing(narrowing_id, release, on, tuple(dropped_territories), tuple(dropped_works))
        self.scope_narrowings.append(narrowing)
        self._apply_to_engine(narrowing)
        return narrowing

    def _apply_to_engine(self, narrowing: ScopeNarrowing) -> None:
        """把缩小范围同步给消耗引擎，撤回日后的使用不再计正常收入。"""
        engine = getattr(self.quotes.analyzer, "engine", None)
        if engine is None or narrowing.release not in engine.resolved_releases:
            return
        resolved = engine.resolved_releases[narrowing.release]
        resolved.withdrawn_on = narrowing.on
        resolved.withdrawn_territories = tuple(sorted(
            set(resolved.withdrawn_territories) | set(narrowing.dropped_territories)
        ))
        resolved.withdrawn_works = tuple(sorted(
            set(resolved.withdrawn_works) | set(narrowing.dropped_works)
        ))

    # ------------------------------------------------------------------
    # 归属裁决影响
    # ------------------------------------------------------------------
    def ownership_ruling_impacts(self) -> list[AmendmentImpact]:
        impacts: list[AmendmentImpact] = []
        rulings = [e for e in self.store.late_evidence if e.kind == "ownership_ruling"]
        for ruling in rulings:
            assert ruling.work
            future_risk = [
                f"work_{ruling.work}_owner_is_{ruling.value}_from_{ruling.on}",
                "new_quotes_require_new_owner_confirmation",
            ]
            contract_actions: list[str] = []
            affected: list[str] = []
            for lic in self.store.licenses.values():
                if lic.work != ruling.work:
                    continue
                if lic.to_date < ruling.on:
                    contract_actions.append(f"license_{lic.id}_ended_before_ruling_keep_as_is")
                    affected.append(lic.id)
                else:
                    # 许可跨越裁决日：裁决日之前的使用仍按旧权利人解释
                    contract_actions.append(
                        f"license_{lic.id}_valid_until_{ruling.on}_under_previous_owner;"
                        f"usage_after_requires_supplemental_authorization_or_compensation"
                    )
                    affected.append(lic.id)
            # 已形成的续约许可（报价生效日在裁决日之后）需新权利人确认
            for quote in self.quotes.quotes.values():
                if quote.status == QUOTE_FORMED and ruling.work in self.quotes.required_works(
                    self.store.requests[quote.request_id]
                ):
                    contract_actions.append(
                        f"formed_license_{quote.effective_license_id}_needs_{ruling.value}_confirmation"
                    )
            impacts.append(
                AmendmentImpact(
                    amendment_id=ruling.id,
                    kind="ownership_ruling",
                    on=ruling.on,
                    subject=ruling.work,
                    future_risk=future_risk,
                    contract_actions=contract_actions,
                    affected_licenses=sorted(set(affected)),
                )
            )
        return impacts

    # ------------------------------------------------------------------
    # 发行范围缩小影响
    # ------------------------------------------------------------------
    def narrowing_impacts(self) -> list[AmendmentImpact]:
        impacts: list[AmendmentImpact] = []
        for narrowing in self.scope_narrowings:
            release = self.store.releases[narrowing.release]
            affected_licenses = []
            contract_actions = [
                f"release_{narrowing.release}_usage_after_{narrowing.on}_excluded_from_billing",
            ]
            wanted_works = set(narrowing.dropped_works)
            for work_id in release.works:
                wanted_works.update(self.lineage.effective_works(work_id))
            for lic in self.store.licenses.values():
                if (
                    lic.work in wanted_works
                    and lic.customer == release.customer
                    and (set(narrowing.dropped_territories) & set(lic.territories)
                         or not narrowing.dropped_territories)
                ):
                    affected_licenses.append(lic.id)
                    contract_actions.append(
                        f"license_{lic.id}_remains_valid_but_no_new_charges_in_narrowed_scope;"
                        "usage_outside_remaining_scope_requires_new_authorization"
                    )
            impacts.append(
                AmendmentImpact(
                    amendment_id=narrowing.id,
                    kind="scope_narrowing",
                    on=narrowing.on,
                    subject=narrowing.release,
                    future_risk=[
                        "future_projections_exclude_narrowed_scope",
                        "renewal_quote_capacity_recomputed",
                    ],
                    contract_actions=contract_actions,
                    affected_licenses=sorted(set(affected_licenses)),
                )
            )
        return impacts

    # ------------------------------------------------------------------
    # 声明修正影响（给出维度与修订号，实际替换由 StatementLedger 完成）
    # ------------------------------------------------------------------
    def statement_revision_impacts(self, ledger) -> list[AmendmentImpact]:
        impacts: list[AmendmentImpact] = []
        latest = ledger.latest_by_dimension()
        superseded = ledger.superseded()
        by_dimension: dict[tuple, list] = {}
        for stmt in superseded:
            by_dimension.setdefault(
                (stmt.customer, stmt.quarter, stmt.territory, stmt.work), []
            ).append(stmt)
        for key, old_statements in sorted(by_dimension.items()):
            new_stmt = latest[key]
            impacts.append(
                AmendmentImpact(
                    amendment_id=new_stmt.id,
                    kind="statement_revision",
                    on=new_stmt.received_on,
                    subject=":".join(key),
                    future_risk=[
                        f"revisions_{[s.id for s in old_statements]}_superseded_by_{new_stmt.id}",
                        "reconciliation_and_projection_use_latest_revision_only",
                    ],
                    contract_actions=[
                        "formed_contract_revenue_kept_under_then_effective_statement_no_retroactive_rewrite"
                    ],
                    affected_licenses=[],
                )
            )
        return impacts

    def all_impacts(self, ledger=None) -> list[AmendmentImpact]:
        impacts = self.ownership_ruling_impacts() + self.narrowing_impacts()
        if ledger is not None:
            impacts.extend(self.statement_revision_impacts(ledger))
        return impacts
