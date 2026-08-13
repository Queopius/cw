from __future__ import annotations

import unittest

from health_app import route


class RouterBaselineTests(unittest.TestCase):
    def test_unknown_route_is_not_found(self) -> None:
        self.assertEqual((404, {"error": "not found"}), route("GET", "/missing"))


if __name__ == "__main__":
    unittest.main()
