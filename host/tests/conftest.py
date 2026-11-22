import pytest


@pytest.fixture(autouse=True)
def no_overpass(monkeypatch):
    """Tests never reach the Overpass API; gap filling falls back as if offline."""
    def offline(boxes):
        raise RuntimeError('network disabled in tests')
    monkeypatch.setattr('bladetag.road_fill.overpass_roads', offline)
