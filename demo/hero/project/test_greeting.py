from __future__ import annotations

import unittest

from greeting import DEFAULT_GREETING


class GreetingBaselineTests(unittest.TestCase):
    def test_default_greeting(self) -> None:
        self.assertEqual("Hello!", DEFAULT_GREETING)


if __name__ == "__main__":
    unittest.main()
