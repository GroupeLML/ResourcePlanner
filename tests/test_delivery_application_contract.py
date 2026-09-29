from __future__ import annotations
from datetime import date
import unittest
from app.application.delivery_contracts import ApprovedPlanningCapacitySourceReadModel, AssetReservedCapacityReadModel, DeliveryAction, DeliveryActorContext, PlanningCapacityProvenance, WorkPackagePlanningCapacityReadModel, delivery_actions_for
from app.domain.delivery import DeliveryItem, DeliveryItemType, DeliveryPlan

class DeliveryApplicationContractTests(unittest.TestCase):

    def setUp(self) -> None:
        self.plan = DeliveryPlan(id='DP-1', work_package_id='WP-1', lead_user_id='USER-LEAD')
        self.story = DeliveryItem(id='STORY-1', delivery_plan_id='DP-1', item_type=DeliveryItemType.STORY, title='Configurer OMI', assignee_user_id='USER-TECH')

    def test_planning_capacity_contract_is_active_approved_read_only_provenance(self) -> None:
        projection = WorkPackagePlanningCapacityReadModel(work_package_id='WP-1', work_package_reference='WP-210', window_start=date(2026, 10, 5), window_end=date(2026, 10, 9), human_reserved_hours=40, total_reserved_hours=40, observed_planning_version=17, approved_sources=(ApprovedPlanningCapacitySourceReadModel(demand_reference='DMO-42', approval_revision_id='APR-7', approved_request_version=3, approved_entry_keys=('LINE-1/PERIOD-1',)),), asset_reserved_capacity=(AssetReservedCapacityReadModel(asset_type_id='LIFT', reserved_days=2),))
        self.assertEqual(projection.provenance, PlanningCapacityProvenance.ACTIVE_APPROVED_PLAN)
        self.assertEqual(projection.total_reserved_hours, 40)
        self.assertEqual(projection.observed_planning_version, 17)
        self.assertEqual(projection.asset_reserved_capacity[0].reserved_days, 2)

    def test_asset_days_are_not_summed_into_human_reserved_hours(self) -> None:
        with self.assertRaisesRegex(ValueError, 'human capacity only'):
            WorkPackagePlanningCapacityReadModel(work_package_id='WP-1', work_package_reference='WP-210', window_start=None, window_end=None, human_reserved_hours=40, total_reserved_hours=42, observed_planning_version=17)

    def test_project_manager_and_admin_get_only_plan_level_conceptual_actions(self) -> None:
        pm_actions = delivery_actions_for(DeliveryActorContext('PM-1', ('PROJECT_MANAGER',)), self.plan)
        admin_actions = delivery_actions_for(DeliveryActorContext('ADMIN-1', ('ADMIN',)), self.plan)
        expected = {DeliveryAction.MANAGE_PLAN_LIFECYCLE, DeliveryAction.ASSIGN_TEAM_LEAD, DeliveryAction.SET_PLAN_PRIORITY_DUE_DATE}
        self.assertTrue(expected.issubset(pm_actions))
        self.assertTrue(expected.issubset(admin_actions))
        self.assertNotIn(DeliveryAction.MANAGE_STRUCTURE, pm_actions)

    def test_team_lead_rights_are_contextual_to_lead_user_id(self) -> None:
        lead_actions = delivery_actions_for(DeliveryActorContext('USER-LEAD', ('TECHNICIAN',)), self.plan)
        other_actions = delivery_actions_for(DeliveryActorContext('OTHER', ('TECHNICIAN',)), self.plan)
        self.assertIn(DeliveryAction.MANAGE_STRUCTURE, lead_actions)
        self.assertIn(DeliveryAction.ESTIMATE_STORIES, lead_actions)
        self.assertNotIn(DeliveryAction.MANAGE_STRUCTURE, other_actions)

    def test_technician_actions_are_limited_to_owned_story(self) -> None:
        technician = DeliveryActorContext('USER-TECH', ('TECHNICIAN',))
        own_actions = delivery_actions_for(technician, self.plan, self.story)
        other_story = DeliveryItem(id='STORY-2', delivery_plan_id='DP-1', item_type=DeliveryItemType.STORY, title='Autre', assignee_user_id='OTHER')
        other_actions = delivery_actions_for(technician, self.plan, other_story)
        self.assertEqual(own_actions, frozenset({DeliveryAction.UPDATE_OWN_STORY_STATUS, DeliveryAction.UPDATE_OWN_REMAINING_HOURS, DeliveryAction.DOCUMENT_OWN_BLOCKAGE}))
        self.assertEqual(other_actions, frozenset())
        self.assertFalse(any(('PLANNING' in action.value for action in DeliveryAction)))
if __name__ == '__main__':
    unittest.main()
