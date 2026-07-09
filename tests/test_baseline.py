import unittest

import numpy as np

from ir_analyzer.core.baseline import baseline_from_points


class BaselineFromPointsTests(unittest.TestCase):
    def test_two_points_use_linear_interpolation_and_extrapolation(self):
        wn = np.array([1000.0, 2000.0, 3000.0, 4000.0])
        ab = np.zeros_like(wn)

        baseline = baseline_from_points(
            wn, ab, [(2000.0, 0.1), (3000.0, 0.2)])

        np.testing.assert_allclose(baseline, [0.0, 0.1, 0.2, 0.3])

    def test_three_points_use_quadratic_interpolation(self):
        wn = np.array([0.0, 0.5, 1.0, 1.5, 2.0])
        ab = np.zeros_like(wn)

        baseline = baseline_from_points(
            wn, ab, [(0.0, 0.0), (1.0, 1.0), (2.0, 4.0)])

        np.testing.assert_allclose(baseline, wn ** 2)

    def test_four_points_use_cubic_interpolation(self):
        wn = np.linspace(0.0, 3.0, 13)
        ab = np.zeros_like(wn)

        baseline = baseline_from_points(
            wn, ab, [(0.0, 0.0), (1.0, 1.0), (2.0, 8.0), (3.0, 27.0)])

        np.testing.assert_allclose(baseline, wn ** 3, atol=1e-12)

    def test_duplicate_and_invalid_points_are_tolerated(self):
        wn = np.array([1000.0, 2000.0, 3000.0])
        ab = np.zeros_like(wn)

        baseline = baseline_from_points(
            wn,
            ab,
            [(1000.0, 0.0), (1000.0, 0.1), (3000.0, 0.3), (np.nan, 1.0)],
        )

        np.testing.assert_allclose(baseline, [0.1, 0.2, 0.3])


if __name__ == '__main__':
    unittest.main()
