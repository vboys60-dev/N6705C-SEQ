import pytest

from n6705c.core.capability import LOAD_MODELS, ChannelCapability


def test_load_models():
    assert LOAD_MODELS == frozenset({"N6791A", "N6792A"})


@pytest.mark.parametrize("model, expected", [
    ("N6791A", True),
    ("N6792A", True),
    ("n6791a", True),
    ("n6792a", True),
    ("N6781A", False),
    ("", False),
])
def test_is_load(model, expected):
    assert ChannelCapability(channel=1, model=model).is_load is expected
