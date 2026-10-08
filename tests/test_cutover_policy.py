from __future__ import annotations

import unittest

from app.domain.cutover_policy import PURE_MODE, normalize_planning_engine_mode


class CutoverPolicyTests(unittest.TestCase):
    def test_old_or_unknown_modes_are_inert_and_normalize_to_pure(self) -> None:
        for value in (None, "", "PURE", "legacy", "guarded_pure", "typo"):
            self.assertEqual(normalize_planning_engine_mode(value), PURE_MODE)


if __name__ == "__main__":
    unittest.main()
