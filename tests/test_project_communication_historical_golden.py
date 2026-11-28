"""Frozen synthetic P0-P4 reference values from the pre-v5 implementation."""
from datetime import date
import hashlib
import unittest

from app.domain.operational_contacts import ContactResolution
from app.domain.project_communication import (
    ProjectCommunicationAsset,
    ProjectCommunicationDay,
    ProjectCommunicationParticipant,
    ProjectCommunicationProject,
    ProjectCommunicationProjection,
    ProjectCommunicationRequestAssignment,
    ProjectCommunicationResource,
    ProjectCommunicationResponsibilityAssignment,
    ProjectCommunicationTask,
)
from app.domain.project_communication_messages import (
    deserialize_project_projection,
    project_projection_fingerprint,
    serialize_project_projection,
)


def historical_fixture(version: int | None) -> ProjectCommunicationProjection:
    def participant(key: str) -> ProjectCommunicationParticipant:
        return ProjectCommunicationParticipant(
            'C-' + key, 'U-' + key, 'Demo ' + key,
            key.lower() + chr(64) + 'invalid.test', None, True,
        )

    day = date(2026, 9, 21)
    asset = ProjectCommunicationAsset(
        'ALLOC-1', 'ASSET-1', 'UNIT-1', 'Demo asset',
        'Demo type', 'Demo category', 'OWNED_SHIFT',
    )
    responsible = ContactResolution(
        'RESOLVED', 'C-RESP', 'Demo responsible', None, None,
        'TASK_RESPONSIBLE', 'TASK-1',
    )
    resource = ProjectCommunicationResource(
        'R-1', 'Demo employee', participant('R-1'), 6.0,
        ('SHIFT-1',), ('Flexible',), ('Confirm\u00e9e',), False, (asset,),
    )
    assignments = () if version is None else (
        ProjectCommunicationRequestAssignment(
            'SHIFT-1', 'REQ-1', 'DEMAND-1', 'DMO-1', 'R-1',
            'Demo employee', 6.0, 'Demo site contact', None,
            include_project_manager_contact_phone=version >= 3,
            project_manager_contact_phone=(
                '514' + '-' + '555' + '-' + '0100' if version >= 3 else None
            ),
            assets=(asset,) if version >= 2 else (),
            standard_hours=8.0 if version >= 2 else None,
            confirmation='Confirm\u00e9e' if version >= 4 else None,
            allocation_type='Flexible' if version >= 4 else None,
            outside_schedule=False if version >= 4 else None,
            task_id='TASK-1' if version >= 4 else None,
            task_code='210' if version >= 4 else None,
            task_description='Demo task' if version >= 4 else None,
        ),
    )
    task = ProjectCommunicationTask(
        'Demo task', ('TASK-1',), ('210',), (responsible,), (resource,),
        (ProjectCommunicationResponsibilityAssignment(
            'SHIFT-1', 'R-1', 'Demo employee', responsible,
        ),), assignments,
    )
    project = ProjectCommunicationProject(
        'PROJECT-1', '1000', 'Demo project', participant('PM'),
        (ProjectCommunicationDay(day, (task,)),),
        project_co_managers=(participant('CO'),),
        requesters=(participant('REQUESTER'),),
    )
    return ProjectCommunicationProjection(
        day, date(2026, 9, 27), (project,), version, version is not None,
    )


# (SHA-256 of canonical serialized UTF-8 bytes, historical projection fingerprint).
# Captured from the pre-v5 algorithms at 49212c5c8629e69a1bc4826f22f76ff1d50d0e8e.
GOLDEN = {
    None: (
        '18a85825fae7e58cbf3a4464110193b9585a4af3386d4e91ab72a05569e6eb81',
        '3ccb2a065ec5f8ce53748d0316aa4f8d2c740db7c582a3e3de254bf094373d53',
    ),
    1: (
        'a08e37290c6b16fba816ccbfe54ed3d7195f3ddf21c261266bf69acae05f5bd5',
        'a73af64fa35402a975074ed4ea61610165c181cef516d00b5c7a7f6c91f00515',
    ),
    2: (
        '601c26bf6a9ddd0733811f15ed89e2b148e73c6f2d6d08c3ef5b13d6cdeae9ce',
        '2f71895a8af64acefded4f47a5166bf4d8c1c255c1db643c4393c3549a0a2da4',
    ),
    3: (
        '2e5ef6f060d558edd4a99c077ad879065883807ce5ccdd257fe5d1a7e4f43480',
        '34f562e9cf51903a0990d0881f7a47119f19d72e8190707c47dfdabc5272b1d2',
    ),
    4: (
        '61906113a434df2242f6915b56e39a2c863ab8254eb99dcc5343db90413a5987',
        '88e208dd7d97a795800b50629b9372ca372f1918b6ee1e77322f4914ae6d6d21',
    ),
}


class HistoricalGoldenTests(unittest.TestCase):
    def test_frozen_p0_p4_serialization_and_fingerprints(self) -> None:
        for version, (serialized_hash, fingerprint) in GOLDEN.items():
            with self.subTest(version=version):
                projection = historical_fixture(version)
                serialized = serialize_project_projection(projection)
                self.assertEqual(hashlib.sha256(serialized.encode('utf-8')).hexdigest(), serialized_hash)
                self.assertEqual(project_projection_fingerprint(projection), fingerprint)

    def test_frozen_p0_p4_round_trips_without_enrichment(self) -> None:
        for version, (serialized_hash, fingerprint) in GOLDEN.items():
            with self.subTest(version=version):
                projection = historical_fixture(version)
                restored = deserialize_project_projection(serialize_project_projection(projection))
                self.assertEqual(restored, projection)
                serialized = serialize_project_projection(restored)
                self.assertEqual(hashlib.sha256(serialized.encode('utf-8')).hexdigest(), serialized_hash)
                self.assertEqual(project_projection_fingerprint(restored), fingerprint)
