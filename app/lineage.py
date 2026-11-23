"""衍生谱系与归属裁决。

规则（见 docs/资料契约.md）：

- 衍生素材获得许可不自动代表原作可用：release 用途必须同时覆盖作品自身与
  所有可达原作；preview 仅表示编辑预览，不产生正式发行权。
- 归属裁决（late_evidence/ownership_ruling）自所列日期生效；裁决日之前的
  既有合同按当时的登记权利人解释，不能用新权利表覆盖历史。
"""
from __future__ import annotations

from .models import LateEvidence, Work


class Lineage:
    def __init__(self, works: dict[str, Work], rulings: list[LateEvidence] | None = None):
        self.works = works
        # 裁决: work -> [(effective_on, new_owner, evidence_id)]
        self._rulings: dict[str, list[tuple[str, str, str]]] = {}
        for evidence in rulings or []:
            if evidence.kind == "ownership_ruling" and evidence.work:
                self._rulings.setdefault(evidence.work, []).append(
                    (evidence.on, evidence.value, evidence.id)
                )
        for entries in self._rulings.values():
            entries.sort()

    def ancestors(self, work_id: str) -> tuple[str, ...]:
        """所有可达原作（不含自身），按谱系深度展开并去重。"""
        seen: list[str] = []
        stack = list(self.works[work_id].parents)
        while stack:
            current = stack.pop(0)
            if current in seen:
                continue
            seen.append(current)
            stack.extend(self.works[current].parents)
        return tuple(seen)

    def effective_works(self, work_id: str) -> tuple[str, ...]:
        """一次使用触及的全部作品：自身 + 全部可达原作。"""
        return (work_id, *self.ancestors(work_id))

    def is_derivative(self, work_id: str) -> bool:
        return bool(self.works[work_id].parents)

    def owner_on(self, work_id: str, on_date: str) -> str:
        """某日期适用的权利人：取不晚于该日期的最后一次裁决，否则取登记权利人。"""
        owner = self.works[work_id].owner
        for effective_on, new_owner, _evidence_id in self._rulings.get(work_id, []):
            if effective_on <= on_date:
                owner = new_owner
            else:
                break
        return owner

    def ruling_history(self, work_id: str) -> list[tuple[str, str, str]]:
        return list(self._rulings.get(work_id, []))

    def has_open_dispute(self, work_id: str) -> bool:
        """是否存在尚未被任何日期消化的归属争议（保留接口用于显式争议清单）。"""
        return work_id in self._rulings
