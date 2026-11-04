"""Maintainer-reviewed donation regression tests can be added below.

Contributors must add a focused test with each proposed fix. A test belongs here
only after the matching production change has been reviewed; the donation worker
does not execute submitted tests on a contributor's computer.
"""

from fractions import Fraction
import unittest

from quick_calculator import calculate


class CalculatorPrecisionTests(unittest.TestCase):
    def test_long_terminating_decimal_is_exact(self):
        expression = "1 / (2^100)^2"
        result = calculate(expression)
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith(expression + " = "))
        displayed = result.split(" = ", 1)[1]
        self.assertIn(".", displayed)
        self.assertNotIn("about", displayed)
        self.assertEqual(Fraction(displayed), Fraction(1, 2**200))
