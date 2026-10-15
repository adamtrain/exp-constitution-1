import pytest

from reflective_stability import stats


def test_sign_flip_exact_p_value():
    # All six values positive: only the all-positive and all-negative sign patterns are as extreme.
    assert stats.sign_flip_test([1, 2, 3, 1, 2, 3]) == pytest.approx(2 / 64)
    assert stats.sign_flip_test([0, 0, 0]) == 1.0


def test_holm_adjustment():
    adjusted = stats.holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adjusted == pytest.approx({"a": 0.03, "c": 0.06, "b": 0.06})


def test_krippendorff_alpha_perfect_and_chance_agreement():
    assert stats.krippendorff_alpha([[1, 1, 1], [0, 0, 0], [-1, -1, -1]], "interval") == 1.0
    assert stats.krippendorff_alpha([["a", "b"], ["b", "a"]]) < 0


def test_krippendorff_alpha_known_value():
    # Krippendorff (2011), nominal example with missing data removed: alpha = 0.743.
    units = [
        [1, 1, 1],
        [2, 2, 3, 2],
        [3, 3, 3, 3],
        [3, 3, 3, 3],
        [2, 2, 2, 2],
        [1, 2, 3, 4],
        [4, 4, 4, 4],
        [1, 1, 2, 1],
        [2, 2, 2, 2],
        [5, 5, 5],
        [1, 1],
        [3],
    ]
    assert stats.krippendorff_alpha(units) == pytest.approx(0.743, abs=0.001)


def test_wilson_interval_contains_proportion():
    low, high = stats.wilson_interval(3, 60)
    assert low < 0.05 < high
