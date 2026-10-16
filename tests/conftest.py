import pytest

from evesim.data.store import StaticData, default_data_path


@pytest.fixture(scope="session")
def data():
    path = default_data_path()
    if not path.exists():
        pytest.skip(f"static data not built ({path}); run `evesim data update`")
    return StaticData.load(path)


@pytest.fixture(scope="session")
def all5(data):
    from evesim.skills.profile import all_level
    return all_level(data, 5).character(data)
