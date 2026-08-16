from mylib import render


def test_render_passes_markup_through():
    assert render("<b>hello</b>") == "<b>hello</b>"
