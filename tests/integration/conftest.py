import pytest

from traffic_cps import SUMO_VERSION
from traffic_cps.cli import DoctorError, check_binary, check_python_clients


@pytest.fixture(scope="session", autouse=True)
def doctor_must_pass():
    """Integration tests require a passing doctor; fail loudly rather than skip."""
    try:
        check_binary("sumo", explicit_path=None, expected=SUMO_VERSION)
        check_binary("netconvert", explicit_path=None, expected=SUMO_VERSION)
        check_python_clients(expected=SUMO_VERSION)
    except DoctorError as exc:
        pytest.fail(f"traffic-cps doctor failed; integration tests cannot run: {exc}", pytrace=False)
