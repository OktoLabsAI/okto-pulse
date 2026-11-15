import pytest

from okto_pulse.community.metrics_limits import validate_window_days

@pytest.mark.parametrize("value",[1,30,400])
def test_valid_window_days(value):
    assert validate_window_days(value) == value


@pytest.mark.parametrize("value",[0,-1,401])
def test_out_of_range(value):
    with pytest.raises(ValueError, match="between 1 and 400"):
        validate_window_days(value)


@pytest.mark.parametrize("value",[True,False])
def test_boolean_values(value):
    with pytest.raises(ValueError):
        validate_window_days(value)


@pytest.mark.parametrize("value",["30",30.0])
def test_invalid_types(value):
    with pytest.raises(ValueError):
        validate_window_days(value)