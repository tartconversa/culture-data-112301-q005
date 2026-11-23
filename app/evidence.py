"""使用证据解释与许可消耗。

处理顺序：

1. 镜像日志已在 :class:`~app.store.MaterialStore` 按 ``origin_event`` 去重，
   同一使用不会因多份镜像重复核算；镜像间覆盖率分歧标记为证据冲突。
2. 结合发行包与迟报证据（late_evidence/release_receipt）解析证据效力：
   正式发行回执 > 打包记录 > 仅预览；缺少回执不视为没有使用。
3. 对每条使用展开实际触及的作品（自身 + 全部可达原作），逐作品判断既有
   release 许可是否在地域与期限内覆盖；preview 用途不产生正式发行权。
4. 未授权使用绝不计为正常收入，只计入争议/补授权清单。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .lineage import Lineage
from .models import (
    BatchObservation,
    ConsumptionLine,
    EVIDENCE_INSUFFICIENT,
    EVIDENCE_PACKAGE,
    EVIDENCE_PREVIEW,
    EVIDENCE_RELEASE_RECEIPT,
    LateEvidence,
    Release,
    UsageEvent,
    WorkRight,
)
from .store import MaterialStore


@dataclass
class ResolvedRelease:
    """合并迟报证据后的发行包视图。"""

    release: Release
    effective_released_on: str | None
    effective_receipt_on: str | None
    receipt_evidence_ids: tuple[str, ...]
    withdrawn_on: str | None = None          # 客户缩小发行范围后的撤回日（由 amendments 登记）
    withdrawn_territories: tuple[str, ...] = ()
    withdrawn_works: tuple[str, ...] = ()

    def is_withdrawn_for(self, event: UsageEvent) -> bool:
        if self.withdrawn_on is None or event.on < self.withdrawn_on:
            return False
        if self.withdrawn_territories and self.release.territory not in self.withdrawn_territories:
            return False
        return True


class ConsumptionEngine:
    def __init__(self, store: MaterialStore, lineage: Lineage, as_of: str | None = None):
        self.store = store
        self.lineage = lineage
        self.as_of = as_of or "9999-12-31"
        self.resolved_releases = self._resolve_releases()
        self._license_index = self._index_licenses()

    # ------------------------------------------------------------------
    # 发行包与迟报证据
    # ------------------------------------------------------------------
    def _resolve_releases(self) -> dict[str, ResolvedRelease]:
        late_receipts: dict[str, list[LateEvidence]] = defaultdict(list)
        for evidence in self.store.late_evidence:
            if evidence.kind == "release_receipt" and evidence.release:
                late_receipts[evidence.release].append(evidence)
        resolved = {}
        for rid, release in self.store.releases.items():
            receipt_ids = tuple(e.id for e in late_receipts.get(rid, []))
            effective_released = release.released_on
            effective_receipt = release.receipt_on
            for evidence in late_receipts.get(rid, []):
                if evidence.on <= self.as_of:
                    # value 为正式发行日；回执到达日取证据 on
                    effective_released = effective_released or evidence.value
                    effective_receipt = effective_receipt or evidence.on
            resolved[rid] = ResolvedRelease(
                release=release,
                effective_released_on=effective_released,
                effective_receipt_on=effective_receipt,
                receipt_evidence_ids=receipt_ids,
            )
        return resolved

    def _index_licenses(self) -> dict[tuple[str, str], list]:
        index: dict[tuple[str, str], list] = defaultdict(list)
        for lic in self.store.licenses.values():
            index[(lic.customer, lic.work)].append(lic)
        return index

    # ------------------------------------------------------------------
    # 权利覆盖
    # ------------------------------------------------------------------
    def _evaluate_right(self, customer: str, territory: str, on_date: str, work_id: str) -> WorkRight:
        owner = self.lineage.owner_on(work_id, on_date)
        right = WorkRight(
            work=work_id,
            owner_at_use=owner,
            is_derivative=self.lineage.is_derivative(work_id),
        )
        candidates = self._license_index.get((customer, work_id), [])
        covering = [
            lic for lic in candidates
            if lic.covers(customer, work_id, territory, "release", on_date)
        ]
        right.license_ids = [lic.id for lic in covering]
        right.covered = bool(covering)
        if not right.covered:
            if not candidates:
                right.reasons.append("no_license_any_scope")
            else:
                purposes = {lic.purpose for lic in candidates}
                if "release" not in purposes:
                    right.reasons.append("preview_only_license_no_release_right")
                for lic in candidates:
                    if lic.purpose == "release":
                        if territory not in lic.territories:
                            right.reasons.append(f"territory_not_licensed:{lic.id}")
                        if not (lic.from_date <= on_date <= lic.to_date):
                            right.reasons.append(f"outside_license_period:{lic.id}")
        # 独占冲突：其他客户在该地域/日期持有独占 release 许可
        for lic in self.store.licenses.values():
            if (
                lic.work == work_id
                and lic.customer != customer
                and lic.purpose == "release"
                and lic.exclusive
                and territory in lic.territories
                and lic.from_date <= on_date <= lic.to_date
            ):
                right.reasons.append(f"exclusive_license_held_by_{lic.customer}:{lic.id}")
        return right

    # ------------------------------------------------------------------
    # 单条使用 -> 消耗解释
    # ------------------------------------------------------------------
    def evidence_strength(self, event: UsageEvent, resolved: ResolvedRelease) -> tuple[str, list[str]]:
        reasons: list[str] = []
        if event.stage == "preview":
            strength = EVIDENCE_PREVIEW
            reasons.append("preview_does_not_grant_release_right")
        elif event.stage == "release" and resolved.effective_receipt_on:
            strength = EVIDENCE_RELEASE_RECEIPT
        else:
            strength = EVIDENCE_PACKAGE
            if event.stage == "release" and not resolved.effective_receipt_on:
                reasons.append("no_receipt_late_channel_reporting_possible")
        if event.coverage_disputed or event.coverage < 1.0:
            reasons.append("coverage_conflict_logs_disagree" if event.coverage_disputed else "partial_log_coverage")
        return strength, reasons

    def build_line(self, event: UsageEvent) -> ConsumptionLine:
        resolved = self.resolved_releases[event.release]
        release = resolved.release
        effective_works: list[str] = []
        for work_id in release.works:
            for wid in self.lineage.effective_works(work_id):
                if wid not in effective_works:
                    effective_works.append(wid)
        # 发行范围缩小：撤回的作品不再属于该使用的有效范围
        withdrawn_works = set(resolved.withdrawn_works)
        effective_works = [w for w in effective_works if w not in withdrawn_works]
        rights = [
            self._evaluate_right(release.customer, release.territory, event.on, wid)
            for wid in effective_works
        ]
        strength, evidence_reasons = self.evidence_strength(event, resolved)
        conflicts = list(evidence_reasons)
        if resolved.is_withdrawn_for(event):
            conflicts.append(
                "scope_narrowed_on_" + resolved.withdrawn_on
                + ":territory=" + release.territory
            )
        unlicensed = [r.work for r in rights if not r.covered]
        if unlicensed:
            conflicts.append("unlicensed_usage:" + ",".join(unlicensed))
        exclusive_conflicts = [
            reason for r in rights for reason in r.reasons if reason.startswith("exclusive_license_held_by_")
        ]
        conflicts.extend(exclusive_conflicts)
        # 预览不产生正式收入；未授权不计正常收入；镜像覆盖分歧保守计收，
        # 只就无争议部分确认收入，存疑部分保留在争议清单。
        billable = (
            event.stage in ("package", "release")
            and not unlicensed
            and not exclusive_conflicts
            and not event.coverage_disputed
            and event.coverage >= 1.0
            and not resolved.is_withdrawn_for(event)
            and strength != EVIDENCE_INSUFFICIENT
        )
        return ConsumptionLine(
            origin_event=event.origin_event,
            release=release.id,
            customer=release.customer,
            territory=release.territory,
            on=event.on,
            stage=event.stage,
            units=event.observed_units,
            evidence_strength=strength,
            coverage_disputed=event.coverage_disputed,
            billable=billable,
            revenue_minor=0,  # 历史许可资料未含单价，金额在 pricing/renewal 层按参考费率计算
            effective_works=effective_works,
            rights=rights,
            conflicts=conflicts,
        )

    def all_lines(self) -> list[ConsumptionLine]:
        return [self.build_line(event) for event in self.store.events.values()]

    # ------------------------------------------------------------------
    # 用量估算区间（覆盖率折算）
    # ------------------------------------------------------------------
    @staticmethod
    def estimated_units(event: UsageEvent) -> tuple[int, float]:
        """返回（观测单位，按最低覆盖率折算的保守上界）。"""
        return event.observed_units, event.observed_units / event.coverage

    # ------------------------------------------------------------------
    # 批次观察状态
    # ------------------------------------------------------------------
    def batches(self) -> list[BatchObservation]:
        events_by_release: dict[str, list[UsageEvent]] = defaultdict(list)
        for event in self.store.events.values():
            events_by_release[event.release].append(event)
        out: list[BatchObservation] = []
        for rid, resolved in sorted(self.resolved_releases.items()):
            release = resolved.release
            events = events_by_release.get(rid, [])
            coverage_complete = all(
                not e.coverage_disputed and e.coverage >= 1.0 for e in events
            )
            reasons: list[str] = []
            if not release.end_on:
                reasons.append("release_period_not_ended")
            if not coverage_complete:
                reasons.append("log_coverage_incomplete_or_disputed")
            status = "complete" if (release.end_on and coverage_complete) else "observing"
            lines = [self.build_line(e) for e in events]
            observed = sum(e.observed_units for e in events)
            billable = sum(l.units for l in lines if l.billable)
            unlicensed = sum(l.units for l in lines if not l.billable and l.stage != "preview")
            disputed = sum(e.observed_units for e in events if e.coverage_disputed)
            out.append(
                BatchObservation(
                    release=rid,
                    customer=release.customer,
                    territory=release.territory,
                    status=status,
                    packaged_on=release.packaged_on,
                    released_on=resolved.effective_released_on,
                    receipt_on=resolved.effective_receipt_on,
                    end_on=release.end_on,
                    coverage_complete=coverage_complete,
                    reasons=reasons,
                    observed_units=observed,
                    billable_units=billable,
                    unlicensed_units=unlicensed,
                    coverage_disputed_units=disputed,
                )
            )
        return out
