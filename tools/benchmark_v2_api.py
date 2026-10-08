from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.application.security import AuthPrincipal, ROLE_ADMIN
from app.infrastructure.sql import Base, create_session_factory, create_sql_engine
from app.infrastructure.sql.asset_models import (
    Asset,
    AssetAllocation,
    AssetRequirement,
    AssetType,
)
from app.infrastructure.sql.models import (
    Project,
    RequestLine,
    Resource,
    ResourceAvailabilityRule,
    ResourceRequirement,
    Shift,
    WorkforceRequest,
    WorkPackage,
)
from app.performance_baseline import (
    DATASET_ORDER,
    aggregate_dataset,
    baseline_payload,
    compare_baselines,
    format_baseline_report,
)
from app.server.performance import read_performance_samples
from app.server import create_api_app
from app.server.security import static_auth_resolver


PERFORMANCE_AUTH_RESOLVER = static_auth_resolver(
    AuthPrincipal.from_roles(
        local_user_id=None,
        issuer="urn:resourceplanner:benchmark",
        subject="benchmark-admin",
        display_name="Benchmark V2",
        email=None,
        roles=(ROLE_ADMIN,),
        auth_mode="test",
    )
)

WINDOW_START = date(2026, 1, 5)
WINDOW_END = date(2026, 1, 9)


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    projects: int
    resources: int
    demands: int
    segments: int
    shifts: int
    assets: int

    def shape(self) -> dict[str, int]:
        return asdict(self)


DATASETS: dict[str, DatasetSpec] = {
    "small": DatasetSpec(
        projects=8, resources=16, demands=32, segments=16, shifts=48, assets=4
    ),
    "medium": DatasetSpec(
        projects=24, resources=48, demands=120, segments=60, shifts=180, assets=8
    ),
    "large": DatasetSpec(
        projects=60, resources=120, demands=360, segments=180, shifts=540, assets=20
    ),
}


ENDPOINTS = (
    "/api/v1/projects?active_only=true",
    "/api/v1/resources?active_only=true",
    "/api/v1/demands",
    f"/api/v1/segments?start={WINDOW_START.isoformat()}&end={WINDOW_END.isoformat()}",
    f"/api/v1/shifts?start={WINDOW_START.isoformat()}&end={WINDOW_END.isoformat()}",
    f"/api/v1/planning/snapshot?start={WINDOW_START.isoformat()}&end={WINDOW_END.isoformat()}",
)


def _seed(database_url: str, spec: DatasetSpec) -> None:
    engine = create_sql_engine(database_url)
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        with factory.begin() as session:
            projects = [
                Project(
                    id=f"PERF-P-{index:04d}",
                    number=f"PERF-{index:04d}",
                    name=f"Projet performance {index:04d}",
                    client=f"Client synthétique {index % 12:02d}",
                    project_manager_name=f"Gestionnaire {index % 8:02d}",
                    status="Actif",
                )
                for index in range(1, spec.projects + 1)
            ]
            session.add_all(projects)

            work_packages = [
                WorkPackage(
                    id=f"PERF-WP-{index:04d}",
                    project_id=project.id,
                    code=f"WP-{index:04d}",
                    name=f"Lot performance {index:04d}",
                    start_date=WINDOW_START,
                    end_date=WINDOW_END,
                    planned_hours=Decimal("120"),
                    status="Planifié",
                )
                for index, project in enumerate(projects, start=1)
            ]
            session.add_all(work_packages)

            resources = [
                Resource(
                    id=f"PERF-R-{index:04d}",
                    external_id=f"PERF-EMP-{index:04d}",
                    name=f"Technicien synthétique {index:04d}",
                    resource_class=("Programmation" if index % 2 else "Installation"),
                    competencies=("SCADA; MES" if index % 2 else "PLC; Réseau"),
                    active=True,
                    sort_order=index,
                )
                for index in range(1, spec.resources + 1)
            ]
            session.add_all(resources)

            asset_type = AssetType(
                id="PERF-ASSET-TYPE",
                code="PERF-EQUIPMENT",
                label="Équipement performance",
                category="EQUIPMENT",
                active=True,
            )
            session.add(asset_type)
            session.flush()
            assets = [
                Asset(
                    id=f"PERF-ASSET-{index:04d}",
                    code=f"PERF-ASSET-{index:04d}",
                    label=f"Actif synthétique {index:04d}",
                    asset_type_id=asset_type.id,
                    active=True,
                )
                for index in range(1, spec.assets + 1)
            ]
            session.add_all(assets)

            session.add_all(
                ResourceAvailabilityRule(
                    id=f"PERF-AV-{index:04d}",
                    resource_id=resource.id,
                    availability_type="Horaire standard",
                    weekdays="Lun,Mar,Mer,Jeu,Ven",
                    start_time=time(8, 0),
                    end_time=time(16, 0),
                    active=True,
                )
                for index, resource in enumerate(resources, start=1)
            )

            demands: list[WorkforceRequest] = []
            for index in range(1, spec.demands + 1):
                project_index = (index - 1) % len(projects)
                resource = resources[(index - 1) % len(resources)]
                submitted = index % 12 == 0
                demands.append(
                    WorkforceRequest(
                        id=f"PERF-D-{index:05d}",
                        legacy_demand_number=f"DMO-PERF-{index:05d}",
                        project_id=projects[project_index].id,
                        work_package_id=work_packages[project_index].id,
                        requester_name=f"Demandeur synthétique {index % 10:02d}",
                        request_type="Projet",
                        priority=("Haute" if index % 7 == 0 else "Normale"),
                        confirmation=("Tentative" if index % 5 == 0 else "Confirmée"),
                        desired_start=WINDOW_START,
                        desired_end=WINDOW_END,
                        description="Charge synthétique déterministe pour baseline V2",
                        resource_count=1,
                        required_competencies=("SCADA" if index % 2 else "PLC"),
                        estimated_hours=Decimal("24"),
                        estimated_days=Decimal("3"),
                        proposed_resource_id=resource.id,
                        status=("Soumise" if submitted else "En planification"),
                    )
                )
            session.add_all(demands)
            session.flush()
            session.add_all(
                [
                    RequestLine(
                        id=demand.id,
                        workforce_request_id=demand.id,
                        position=0,
                        kind="WORKFORCE",
                        slot_count=max(int(demand.resource_count or 1), 1),
                        required_competencies_snapshot=demand.required_competencies,
                        desired_start=demand.desired_start,
                        desired_end=demand.desired_end,
                        desired_active_days=demand.estimated_days,
                        estimated_hours=demand.estimated_hours,
                        confirmation=demand.confirmation,
                        work_package_id=demand.work_package_id,
                        proposed_resource_id=demand.proposed_resource_id,
                        description=demand.description,
                        active=True,
                    )
                    for demand in demands
                ]
            )
            session.flush()

            requirements: list[ResourceRequirement] = []
            for index in range(1, spec.segments + 1):
                demand = demands[(index - 1) % len(demands)]
                resource = resources[(index - 1) % len(resources)]
                requirements.append(
                    ResourceRequirement(
                        id=f"PERF-S-{index:05d}",
                        legacy_segment_id=f"SEG-PERF-{index:05d}",
                        project_id=demand.project_id,
                        workforce_request_id=demand.id,
                        source_request_line_id=demand.id,
                        assigned_resource_id=resource.id,
                        start_date=WINDOW_START,
                        end_date=WINDOW_END,
                        planned_hours=Decimal("24"),
                        desired_active_days=3,
                        load_profile="UNIFORM",
                        status="Planifié",
                        description="Segment synthétique baseline",
                        required_competency=("SCADA" if index % 2 else "PLC"),
                        planning_type="Flexible",
                        priority="Normale",
                        confirmation=demand.confirmation,
                        origin="REQUEST",
                    )
                )
            session.add_all(requirements)
            session.flush()

            shifts: list[Shift] = []
            for index in range(1, spec.shifts + 1):
                requirement = requirements[(index - 1) % len(requirements)]
                resource = resources[(index - 1) % len(resources)]
                shifts.append(
                    Shift(
                        id=f"PERF-A-{index:06d}",
                        legacy_allocation_id=f"ALL-PERF-{index:06d}",
                        resource_requirement_id=requirement.id,
                        resource_id=resource.id,
                        work_date=WINDOW_START + timedelta(days=(index - 1) % 5),
                        hours=Decimal("8"),
                        allocation_type="Flexible",
                        source="AUTO",
                        locked=False,
                        outside_standard_hours=False,
                    )
                )
            session.add_all(shifts)

            asset_requirements = [
                AssetRequirement(
                    id=f"PERF-AREQ-{index:04d}",
                    project_id=projects[(index - 1) % len(projects)].id,
                    origin="PROJECT_DIRECT",
                    asset_type_id=asset_type.id,
                    start_date=WINDOW_START,
                    end_date=WINDOW_END,
                    status="Planifié",
                )
                for index in range(1, spec.assets + 1)
            ]
            session.add_all(asset_requirements)
            session.flush()
            session.add_all(
                AssetAllocation(
                    id=f"PERF-AALLOC-{index:04d}",
                    asset_requirement_id=requirement.id,
                    asset_id=assets[(index - 1) % len(assets)].id,
                    operator_resource_id=resources[(index - 1) % len(resources)].id,
                    start_date=WINDOW_START,
                    end_date=WINDOW_END,
                    locked=True,
                    source="MANUAL",
                )
                for index, requirement in enumerate(asset_requirements, start=1)
            )
    finally:
        engine.dispose()


def _exercise(client: TestClient, *, iterations: int) -> None:
    for endpoint in ENDPOINTS:
        for _ in range(iterations):
            response = client.get(endpoint)
            response.raise_for_status()


def run_dataset(name: str, *, iterations: int, root: Path) -> dict[str, object]:
    spec = DATASETS[name]
    database_path = root / f"{name}.db"
    log_path = root / f"{name}.performance.jsonl"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    _seed(database_url, spec)
    app = create_api_app(
        database_url,
        auth_resolver=PERFORMANCE_AUTH_RESOLVER,
        performance_log_path=log_path,
    )
    with TestClient(app) as client:
        # Warm caches and SQLAlchemy compilation before collecting comparable samples.
        for endpoint in ENDPOINTS:
            response = client.get(endpoint)
            response.raise_for_status()
        if log_path.exists():
            log_path.unlink()
        _exercise(client, iterations=iterations)

    samples = read_performance_samples(
        path=log_path,
        limit=(len(ENDPOINTS) * iterations) + 10,
    )
    return aggregate_dataset(samples, dataset=name, shape=spec.shape())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark reproductible de l'API V2 sur des datasets SQLite synthétiques."
    )
    parser.add_argument(
        "--dataset",
        action="append",
        choices=DATASET_ORDER,
        dest="datasets",
        help="Dataset à exécuter; répétable. Par défaut: small, medium, large.",
    )
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--compare",
        type=Path,
        help="Baseline JSON précédente à comparer au run courant.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Retourne un code non nul lorsqu'un budget structurel est dépassé.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.iterations < 2:
        raise SystemExit("--iterations doit être >= 2 pour produire des percentiles utiles")
    selected = tuple(args.datasets or DATASET_ORDER)
    with TemporaryDirectory(prefix="resourceplanner-v2-perf-") as directory:
        root = Path(directory)
        reports = [
            run_dataset(name, iterations=args.iterations, root=root)
            for name in selected
        ]
    payload = baseline_payload(reports)
    if args.compare is not None:
        before = json.loads(args.compare.read_text(encoding="utf-8"))
        if not isinstance(before, dict):
            raise SystemExit("--compare doit pointer vers un objet JSON de baseline")
        payload["comparison"] = compare_baselines(before, payload)
    print(format_baseline_report(payload))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"\nJSON: {args.output}")
    return 1 if args.ci and not payload["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
