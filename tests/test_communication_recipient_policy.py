from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
import unittest

from app.domain.communication_recipient_policy import RecipientPolicyPopulation
from app.domain.superintendent_population import SuperintendentEmployee, SuperintendentPopulation
from app.domain.superintendent_recipient_resolution import AddressFailure, AddressResolution


def mailbox(local: str) -> str:
    return local + chr(64) + "invalid.test"


class RecipientPolicyPopulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.population = SuperintendentPopulation("COMP-1", (
            SuperintendentEmployee("R-3", "Demo 3", "bad-address"),
            SuperintendentEmployee("R-1", "Demo 1", " " + mailbox("valid") + " "),
            SuperintendentEmployee("R-2", "Demo 2", None),
        ))
        self.context = RecipientPolicyPopulation.capture(self.population)

    def test_complete_population_includes_unusable_addresses(self) -> None:
        self.assertTrue(self.context.population_complete)
        self.assertEqual([row.employee.resource_id for row in self.context.employees], ["R-1", "R-2", "R-3"])
        self.assertEqual(self.context.employees[0].resolution.address, mailbox("valid"))
        self.assertEqual(self.context.employees[0].employee.email, " " + mailbox("valid") + " ")
        self.assertEqual([(row.resource_id, row.reason) for row in self.context.address_diagnostics], [
            ("R-2", AddressFailure.MISSING), ("R-3", AddressFailure.INVALID),
        ])
        self.assertEqual(self.context.to_population().employees, tuple(sorted(self.population.employees, key=lambda row: row.resource_id)))
        with self.assertRaises(FrozenInstanceError):
            self.context.population_complete = False

    def test_valid_empty_and_binding_failures_are_distinct(self) -> None:
        empty = RecipientPolicyPopulation.capture(SuperintendentPopulation("COMP-1"))
        self.assertTrue(empty.population_complete)
        self.assertEqual(empty.employees, ())
        for competency, reason in (
            (None, "SUPERINTENDENT_BINDING_MISSING"),
            (" COMP-1", "SUPERINTENDENT_BINDING_INVALID"),
            ("COMP-1", "SUPERINTENDENT_BINDING_INACTIVE"),
        ):
            with self.subTest(reason=reason):
                failed = RecipientPolicyPopulation.capture(SuperintendentPopulation(competency, diagnostic=reason))
                self.assertFalse(failed.population_complete)
                self.assertNotEqual(failed, empty)
                self.assertEqual(RecipientPolicyPopulation.from_payload(failed.to_payload()), failed)

    def test_canonical_order_and_json_round_trip(self) -> None:
        reversed_population = replace(self.population, employees=tuple(reversed(self.population.employees)))
        self.assertEqual(RecipientPolicyPopulation.capture(reversed_population).to_payload(), self.context.to_payload())
        restored = RecipientPolicyPopulation.from_payload(json.loads(json.dumps(self.context.to_payload())))
        self.assertEqual(restored, self.context)
        corrected = replace(self.population, employees=(
            self.population.employees[0], self.population.employees[1],
            replace(self.population.employees[2], email=mailbox("corrected")),
        ))
        self.assertNotEqual(RecipientPolicyPopulation.capture(corrected).to_payload(), restored.to_payload())
        self.assertIsNone(restored.employees[1].employee.email)

    def test_rejects_inconsistent_population_and_duplicate_identity(self) -> None:
        for population in (
            SuperintendentPopulation(None),
            replace(self.population, diagnostic="SUPERINTENDENT_BINDING_INVALID"),
            replace(self.population, employees=self.population.employees + (self.population.employees[0],)),
            SuperintendentPopulation("COMP-1", diagnostic="UNKNOWN"),
        ):
            with self.subTest(population=population):
                with self.assertRaises(ValueError):
                    RecipientPolicyPopulation.capture(population)
        with self.assertRaises(ValueError):
            replace(self.context, employees=(replace(self.context.employees[0], resolution=AddressResolution(None)),))

    def test_rejects_missing_unknown_and_contradictory_serialized_facts(self) -> None:
        payload = self.context.to_payload()
        mutations = (
            lambda value: value.pop("employees"),
            lambda value: value.update(policy_version=True),
            lambda value: value.update(policy_version=2),
            lambda value: value.update(policy_id="OTHER"),
            lambda value: value.update(address_authority="BUSINESS_CONTACT_EMAIL"),
            lambda value: value.update(population_complete=1),
            lambda value: value.update(binding_diagnostic="SUPERINTENDENT_BINDING_INVALID"),
            lambda value: value.update(competency_id=None),
            lambda value: value.update(population_complete=False),
            lambda value: value.update(employees=list(reversed(value["employees"]))),
            lambda value: value["employees"][1].update(resolved_address=mailbox("invented")),
            lambda value: value["employees"][1].update(address_failure=None),
            lambda value: value["employees"][1].update(address_failure="UNKNOWN"),
            lambda value: value["employees"][1].pop("resource_id"),
            lambda value: value["employees"][1].update(extra=True),
        )
        for mutation in mutations:
            invalid = deepcopy(payload)
            mutation(invalid)
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    RecipientPolicyPopulation.from_payload(invalid)
