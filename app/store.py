"""合成资料读取、指纹校验与本地状态持久化。

资料读取复用 ``tools/check_materials.py`` 的校验口径（指纹、行数、字段类型、
直接外键），保证应用只消费符合 ``docs/资料契约.md`` 的记录。

应用自身产生的状态以 JSON 文件保存在 state/（已从 Git 排除），采用只追加的
事件账本 + 派生快照结构，支持反复导入声明而不改动原始资料。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Iterable, Optional

from . import models

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STATE_DIR = ROOT / "state"
OUTPUTS_DIR = ROOT / "outputs"


class DataIntegrityError(RuntimeError):
    """资料指纹或结构校验失败。"""


def _load_check_module():
    spec = importlib.util.spec_from_file_location(
        "material_check", ROOT / "tools" / "check_materials.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class MaterialStore:
    """只读资料仓库：载入并校验全部合成资料表。"""

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self.catalog = json.loads((self.root / "data" / "catalog.json").read_text(encoding="utf-8"))
        self._raw: dict[str, list[dict]] = {}
        self._load_and_verify()
        self.works = self._build_works()
        self.licenses = self._build_licenses()
        self.releases = self._build_releases()
        self.events = self._build_events()
        self.statements = self._build_statements()
        self.guarantees = self._build_guarantees()
        self.terms = self._build_terms()
        self.requests = self._build_requests()
        self.authorization_events = self._build_authorization_events()
        self.late_evidence = self._build_late_evidence()

    def _load_and_verify(self) -> None:
        checker = _load_check_module()
        try:
            checker.check(self.root)
        except ValueError as exc:  # 重新抛出为应用层异常类型
            raise DataIntegrityError(str(exc)) from exc
        for name in self.catalog["tables"]:
            self._raw[name] = _read_jsonl(self.root / "data" / (name + ".jsonl"))

    def rows(self, name: str) -> list[dict]:
        return list(self._raw[name])

    def fingerprint(self, name: str) -> str:
        return self.catalog["tables"][name]["sha256"]

    def data_fingerprints(self) -> dict[str, str]:
        return {name: spec["sha256"] for name, spec in self.catalog["tables"].items()}

    def _build_works(self) -> dict[str, models.Work]:
        return {
            r["id"]: models.Work(r["id"], r["kind"], tuple(r["parents"]), r["owner"])
            for r in self._raw["works"]
        }

    def _build_licenses(self) -> dict[str, models.License]:
        out = {}
        for r in self._raw["licenses"]:
            out[r["id"]] = models.License(
                r["id"], r["work"], r["customer"], tuple(r["territories"]), r["purpose"],
                r["from"], r["to"], r["exclusive"], r["sublicense"], r["guarantee_pool"],
            )
        return out

    def _build_releases(self) -> dict[str, models.Release]:
        out = {}
        for r in self._raw["releases"]:
            out[r["id"]] = models.Release(
                r["id"], r["customer"], r["territory"], tuple(r["works"]),
                r["packaged_on"], r["released_on"], r["receipt_on"], r["end_on"],
            )
        return out

    def _build_events(self) -> dict[str, models.UsageEvent]:
        """镜像日志按 origin_event 去重，保留镜像身份与覆盖分歧。"""
        grouped: dict[str, list[dict]] = {}
        for r in self._raw["usage_logs"]:
            grouped.setdefault(r["origin_event"], []).append(r)
        events = {}
        for origin, mirrors in grouped.items():
            head = mirrors[0]
            coverages = {m["coverage"] for m in mirrors}
            units = {m["observed_units"] for m in mirrors}
            stages = {m["stage"] for m in mirrors}
            if len(units) != 1 or len(stages) != 1:
                raise DataIntegrityError(f"镜像日志内容不一致: {origin}")
            events[origin] = models.UsageEvent(
                origin_event=origin,
                release=head["release"],
                on=head["on"],
                stage=head["stage"],
                observed_units=head["observed_units"],
                coverage=max(coverages),
                mirror_ids=tuple(sorted(m["id"] for m in mirrors)),
                coverage_disputed=len(coverages) > 1,
            )
        return events

    def _build_statements(self) -> list[models.Statement]:
        return [
            models.Statement(
                r["id"], r["customer"], r["quarter"], r["territory"], r["work"],
                r["units"], r["revision"], r["received_on"],
            )
            for r in self._raw["statements"]
        ]

    def _build_guarantees(self) -> dict[str, models.GuaranteePool]:
        return {
            r["id"]: models.GuaranteePool(
                r["id"], r["currency"], r["limit_minor"], r["reserved_minor"],
                tuple(r["works"]), tuple(r["territories"]),
            )
            for r in self._raw["guarantees"]
        }

    def _build_terms(self) -> dict[str, models.Term]:
        return {
            r["id"]: models.Term(
                r["id"], r["kind"], r["unit_minor"], r["minimum_minor"],
                r["cap_minor"], tuple(r["allowed_with"]),
            )
            for r in self._raw["terms"]
        }

    def _build_requests(self) -> dict[str, models.RenewalRequest]:
        out = {}
        for r in self._raw["renewal_requests"]:
            out[r["id"]] = models.RenewalRequest(
                r["id"], r["customer"], tuple(r["works"]), r["territory"], r["purpose"],
                r["from"], r["to"], r["exclusive"], r["alternative_group"], r["expires_on"],
            )
        return out

    def _build_authorization_events(self) -> list[models.AuthorizationEvent]:
        return [
            models.AuthorizationEvent(
                r["id"], r["request"], r["party"], r["kind"], r["on"], r["work"], r["territory"],
            )
            for r in self._raw["authorization_events"]
        ]

    def _build_late_evidence(self) -> list[models.LateEvidence]:
        return [
            models.LateEvidence(
                r["id"], r["kind"], r["release"], r["work"], r["on"], r["value"],
            )
            for r in self._raw["late_evidence"]
        ]


# ---------------------------------------------------------------------------
# 本地状态：事件账本（只追加）+ 派生快照
# ---------------------------------------------------------------------------

LEDGER_FILE = "ledger.jsonl"
SNAPSHOT_FILE = "snapshot.json"


class StateStore:
    """应用本地状态。

    账本记录三类应用动作：
      - statement_import: 追加导入的声明（含修订）
      - quote_action: 报价生命周期动作（issue/expire/withdraw/register_event）
      - amendment: 资料修正登记
    快照保存报价/合同派生状态，重放账本即可重建。
    """

    def __init__(self, state_dir: Path = STATE_DIR):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path = self.state_dir / LEDGER_FILE
        self.snapshot_path = self.state_dir / SNAPSHOT_FILE

    def append(self, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("seq", self._next_seq())
        with self.ledger_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def _next_seq(self) -> int:
        return sum(1 for _ in self.read_ledger()) + 1

    def read_ledger(self) -> list[dict]:
        if not self.ledger_path.exists():
            return []
        return _read_jsonl(self.ledger_path)

    def save_snapshot(self, snapshot: dict) -> None:
        self.snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def load_snapshot(self) -> Optional[dict]:
        if not self.snapshot_path.exists():
            return None
        return json.loads(self.snapshot_path.read_text(encoding="utf-8"))

    def reset(self) -> None:
        for path in (self.ledger_path, self.snapshot_path):
            if path.exists():
                path.unlink()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
