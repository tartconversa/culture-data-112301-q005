"""季度使用声明的修订管理与对账。

- 同一 (customer, quarter, territory, work) 只取最高 revision；被取代的声明
  保留身份但不参与对账，避免重复计入。
- 日志观察按同一维度聚合（事件已去重），与声明比较差异，区分多报、少报与
  无观察。少报可能来自渠道迟报或日志覆盖下降，不直接判定客户违约。
- 支持反复导入额外声明文件：原始资料只读，新声明进入 state 账本。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .models import ConsumptionLine, Statement, StatementReconciliation
from .store import MaterialStore

DIMENSION = tuple[str, str, str, str]  # customer, quarter, territory, work


def quarter_of(date_str: str) -> str:
    year, month, _ = date_str.split("-")
    return f"{year}Q{(int(month) - 1) // 3 + 1}"


class StatementLedger:
    def __init__(self, base: list[Statement]):
        self._statements: list[Statement] = list(base)

    def import_file(self, path: str | Path) -> int:
        """导入额外声明 JSONL，返回新增条数。同 ID 记录被拒绝（身份不可复用）。"""
        existing = {s.id for s in self._statements}
        added = 0
        for raw in map(json.loads, Path(path).read_text(encoding="utf-8").splitlines()):
            if not raw.get("id") or raw["id"] in existing:
                raise ValueError(f"声明身份重复或缺失: {raw.get('id')}")
            self._statements.append(
                Statement(
                    raw["id"], raw["customer"], raw["quarter"], raw["territory"],
                    raw["work"], raw["units"], raw["revision"], raw["received_on"],
                    source=str(path),
                )
            )
            existing.add(raw["id"])
            added += 1
        return added

    def latest_by_dimension(self) -> dict[DIMENSION, Statement]:
        latest: dict[DIMENSION, Statement] = {}
        for stmt in self._statements:
            key = (stmt.customer, stmt.quarter, stmt.territory, stmt.work)
            if key not in latest or stmt.revision > latest[key].revision:
                latest[key] = stmt
        return latest

    def superseded(self) -> list[Statement]:
        latest = self.latest_by_dimension()
        return [s for s in self._statements if latest[(s.customer, s.quarter, s.territory, s.work)].id != s.id]

    def all(self) -> list[Statement]:
        return list(self._statements)


def observed_by_dimension(lines: list[ConsumptionLine], effective_works_by_release: dict[str, list[str]]):
    """把去重后的使用事件按声明维度聚合。

    一条发行包使用同时触及多个作品（含可达原作），每个触及作品各承担该事件
    的观测单位——与声明按作品申报的口径一致。preview 事件不纳入正式对账量，
    只保留在消耗解释中。
    """
    totals: dict[DIMENSION, int] = defaultdict(int)
    for line in lines:
        if line.stage == "preview":
            continue
        quarter = quarter_of(line.on)
        for work_id in effective_works_by_release[line.release]:
            totals[(line.customer, quarter, line.territory, work_id)] += line.units
    return totals


def reconcile(
    ledger: StatementLedger,
    observed: dict[DIMENSION, int],
) -> list[StatementReconciliation]:
    results: list[StatementReconciliation] = []
    superseded_ids: dict[DIMENSION, list[str]] = defaultdict(list)
    for stmt in ledger.superseded():
        superseded_ids[(stmt.customer, stmt.quarter, stmt.territory, stmt.work)].append(stmt.id)
    for key, stmt in sorted(ledger.latest_by_dimension().items()):
        observed_units = observed.get(key, 0)
        diff = stmt.units - observed_units
        if observed_units == 0:
            status = "no_observation"
        elif diff > 0:
            status = "overstated"
        elif diff < 0:
            status = "understated"
        else:
            status = "matched"
        results.append(
            StatementReconciliation(
                customer=stmt.customer,
                quarter=stmt.quarter,
                territory=stmt.territory,
                work=stmt.work,
                latest_revision=stmt.revision,
                statement_units=stmt.units,
                observed_units=observed_units,
                difference=diff,
                status=status,
                superseded_statement_ids=superseded_ids.get(key, []),
            )
        )
    return results


def build_ledger(store: MaterialStore) -> StatementLedger:
    return StatementLedger(store.statements)
