from __future__ import annotations
from dataclasses import fields
from datetime import datetime
import inspect
import unittest
from app.domain.delivery import DeliveryItem, DeliveryItemStatus, DeliveryItemType, DeliveryMetricState, DeliveryPlan, DeliveryPlanStatus, delivery_forecast, delivery_progress, reestimate_story, transition_delivery_plan, transition_story_status, update_story_remaining_hours, validate_delivery_hierarchy, validate_non_archived_plan_uniqueness

def story(identifier: str, *, parent_id: str | None=None, status: DeliveryItemStatus=DeliveryItemStatus.BACKLOG, current: float | None=8, reference: float | None=None, remaining: float | None=8, assignee: str | None=None) -> DeliveryItem:
    return DeliveryItem(id=identifier, delivery_plan_id='DP-1', item_type=DeliveryItemType.STORY, parent_id=parent_id, title=identifier, status=status, assignee_user_id=assignee, current_estimate_hours=current, reference_estimate_hours=reference, remaining_hours=remaining)

class DeliveryDomainTests(unittest.TestCase):

    def setUp(self) -> None:
        self.plan = DeliveryPlan(id='DP-1', work_package_id='WP-1')

    def test_plan_lifecycle_and_identity_are_stable(self) -> None:
        activated = transition_delivery_plan(self.plan, DeliveryPlanStatus.ACTIVE, occurred_at=datetime(2026, 9, 28, 12, 0))
        archived = transition_delivery_plan(activated, DeliveryPlanStatus.ARCHIVED)
        self.assertEqual(activated.id, self.plan.id)
        self.assertEqual(activated.work_package_id, self.plan.work_package_id)
        self.assertEqual(archived.status, DeliveryPlanStatus.ARCHIVED)
        with self.assertRaises(ValueError):
            transition_delivery_plan(self.plan, DeliveryPlanStatus.ARCHIVED)

    def test_only_one_non_archived_plan_is_allowed_per_work_package(self) -> None:
        second = DeliveryPlan(id='DP-2', work_package_id='WP-1')
        with self.assertRaisesRegex(ValueError, 'one non-archived'):
            validate_non_archived_plan_uniqueness((self.plan, second))
        archived = DeliveryPlan(id='DP-OLD', work_package_id='WP-1', status=DeliveryPlanStatus.ARCHIVED)
        validate_non_archived_plan_uniqueness((archived, self.plan))

    def test_epic_can_parent_stories_but_story_can_never_parent_child(self) -> None:
        epic = DeliveryItem(id='E-1', delivery_plan_id=self.plan.id, item_type=DeliveryItemType.EPIC, title='Epic')
        child = story('S-1', parent_id=epic.id)
        validate_delivery_hierarchy(self.plan, (epic, child))
        invalid_parent = story('S-PARENT')
        invalid_child = story('S-CHILD', parent_id=invalid_parent.id)
        with self.assertRaisesRegex(ValueError, 'EPIC parent'):
            validate_delivery_hierarchy(self.plan, (invalid_parent, invalid_child))
        nested_epic = DeliveryItem(id='E-2', delivery_plan_id=self.plan.id, item_type=DeliveryItemType.EPIC, parent_id='E-1', title='Nested')
        with self.assertRaisesRegex(ValueError, 'EPIC cannot have a parent'):
            validate_delivery_hierarchy(self.plan, (epic, nested_epic))

    def test_reference_estimate_is_captured_on_first_committed_transition_only(self) -> None:
        backlog = story('S-1', current=10, reference=None)
        committed = transition_story_status(backlog, DeliveryItemStatus.TODO)
        reestimated = reestimate_story(committed, 18)
        blocked = transition_story_status(reestimated, DeliveryItemStatus.BLOCKED)
        self.assertEqual(committed.reference_estimate_hours, 10)
        self.assertEqual(reestimated.current_estimate_hours, 18)
        self.assertEqual(blocked.reference_estimate_hours, 10)
        cancelled_from_backlog = transition_story_status(story('S-2', current=5), DeliveryItemStatus.CANCELLED)
        self.assertIsNone(cancelled_from_backlog.reference_estimate_hours)

    def test_first_estimate_after_commit_becomes_reference_once(self) -> None:
        committed_unestimated = transition_story_status(story('S-LATE', current=None), DeliveryItemStatus.TODO)
        first_estimate = reestimate_story(committed_unestimated, 6)
        later_estimate = reestimate_story(first_estimate, 9)
        self.assertEqual(first_estimate.reference_estimate_hours, 6)
        self.assertEqual(later_estimate.reference_estimate_hours, 6)
        self.assertEqual(later_estimate.current_estimate_hours, 9)

    def test_progress_is_done_only_and_stable_after_current_reestimate(self) -> None:
        done = story('DONE', status=DeliveryItemStatus.DONE, reference=8, current=8)
        blocked = story('BLOCKED', status=DeliveryItemStatus.BLOCKED, reference=8, current=8)
        in_progress = story('INPROG', status=DeliveryItemStatus.IN_PROGRESS, reference=8, current=8)
        todo = story('TODO', status=DeliveryItemStatus.TODO, reference=8, current=8)
        backlog = story('BACKLOG', status=DeliveryItemStatus.BACKLOG, reference=8, current=8)
        before = delivery_progress((done, blocked, in_progress, todo, backlog))
        after = delivery_progress((done, reestimate_story(blocked, 80), in_progress, todo, backlog))
        self.assertEqual(before.progress_ratio, 0.2)
        self.assertEqual(after.progress_ratio, before.progress_ratio)
        self.assertEqual(after.total_reference_hours, 40)
        self.assertEqual(after.completed_reference_hours, 8)

    def test_unestimated_story_has_explicit_progress_coverage_and_no_fake_ratio(self) -> None:
        missing = story('MISSING', current=None, reference=None)
        metric = delivery_progress((missing,))
        self.assertEqual(metric.state, DeliveryMetricState.NO_REFERENCE_ESTIMATE)
        self.assertIsNone(metric.progress_ratio)
        self.assertEqual(metric.coverage_ratio, 0.0)
        self.assertEqual(metric.unestimated_story_ids, ('MISSING',))
        partial = delivery_progress((missing, story('KNOWN', status=DeliveryItemStatus.DONE, reference=4)))
        self.assertEqual(partial.state, DeliveryMetricState.PARTIAL_COVERAGE)
        self.assertEqual(partial.progress_ratio, 1.0)
        self.assertEqual(partial.coverage_ratio, 0.5)

    def test_remaining_hours_changes_forecast_without_changing_progress(self) -> None:
        item = story('S-1', status=DeliveryItemStatus.IN_PROGRESS, reference=10, remaining=7)
        progress_before = delivery_progress((item,))
        forecast_before = delivery_forecast((item,))
        changed = update_story_remaining_hours(item, 3)
        progress_after = delivery_progress((changed,))
        forecast_after = delivery_forecast((changed,))
        self.assertEqual(progress_before, progress_after)
        self.assertEqual(forecast_before.total_remaining_hours, 7)
        self.assertEqual(forecast_after.total_remaining_hours, 3)
        self.assertEqual(changed.reference_estimate_hours, 10)

    def test_cancelled_story_is_retained_but_excluded_from_current_aggregates(self) -> None:
        cancelled = story('CANCELLED', status=DeliveryItemStatus.CANCELLED, reference=100, remaining=100)
        active = story('ACTIVE', status=DeliveryItemStatus.IN_PROGRESS, reference=10, remaining=4)
        progress = delivery_progress((cancelled, active))
        forecast = delivery_forecast((cancelled, active))
        self.assertEqual(progress.total_reference_hours, 10)
        self.assertEqual(progress.cancelled_story_count, 1)
        self.assertEqual(forecast.total_remaining_hours, 4)
        self.assertEqual(forecast.cancelled_story_count, 1)

    def test_done_and_cancelled_are_terminal_for_forecast(self) -> None:
        items = (story('DONE', status=DeliveryItemStatus.DONE, reference=8, remaining=5), story('CANCELLED', status=DeliveryItemStatus.CANCELLED, reference=8, remaining=5))
        forecast = delivery_forecast(items)
        self.assertEqual(forecast.total_remaining_hours, 0)
        self.assertEqual(forecast.open_story_count, 0)

    def test_missing_remaining_hours_is_explicit_forecast_diagnostic(self) -> None:
        metric = delivery_forecast((story('S-1', remaining=None),))
        self.assertEqual(metric.state, DeliveryMetricState.NO_REMAINING_ESTIMATE)
        self.assertIsNone(metric.total_remaining_hours)
        self.assertEqual(metric.missing_remaining_story_ids, ('S-1',))

    def test_delivery_mutations_do_not_depend_on_planning_version_or_mutate_external_state(self) -> None:
        planning_state = {'shifts': ('SHIFT-1',), 'planning_version': 42}
        work_package = {'id': 'WP-1', 'planned_hours': 80}
        before_planning = dict(planning_state)
        before_work_package = dict(work_package)
        item = transition_story_status(story('S-1'), DeliveryItemStatus.IN_PROGRESS)
        item = reestimate_story(item, 12)
        item = update_story_remaining_hours(item, 6)
        self.assertEqual(planning_state, before_planning)
        self.assertEqual(work_package, before_work_package)
        self.assertEqual(item.id, 'S-1')
        self.assertNotIn('planning_version', {field.name for field in fields(DeliveryPlan)})
        self.assertNotIn('planning_version', {field.name for field in fields(DeliveryItem)})
        for function in (transition_story_status, reestimate_story, update_story_remaining_hours):
            self.assertNotIn('planning_version', inspect.signature(function).parameters)
if __name__ == '__main__':
    unittest.main()
