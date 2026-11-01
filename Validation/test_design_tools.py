"""Local design helpers keep source findings and color math inspectable."""

import pytest

from design_tools import check_color_contrast, luminance


def test_black_and_white_have_maximum_contrast():
    result = check_color_contrast("#000000", "#FFFFFF")
    assert result["ratio"] == 21
    assert result["normal_text_AAA"] is True


def test_same_color_fails_text_contrast():
    result = check_color_contrast("#444444", "#444444")
    assert result["ratio"] == 1
    assert result["normal_text_AA"] is False


def test_rejects_non_hex_color():
    with pytest.raises(ValueError):
        luminance("red")
