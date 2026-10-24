import httpx
import pytest

from tests.test_client import UUID, asset_response, make_client


def test_asset_query_requests_prices():
    from splice_curator import queries

    assert "asset_prices" in queries.ASSET_FILES.template


@pytest.mark.parametrize("prices, expected", [
    ([{"amount": 1, "currency": "Credits"}], 1),
    ([{"amount": "3", "currency": "credit"}], 3),
    ([{"amount": 5, "currency": "USD"}, {"amount": 2, "currency": "Credits"}], 2),
    ([{"amount": 1.5, "currency": "Credits"}], None),
    ([{"amount": "x", "currency": "Credits"}], None),
    ([{"amount": 1, "currency": "USD"}], None),
    ([], None),
    (None, None),
])
def test_asset_files_parses_price(session, throttle, config, prices, expected):
    body = asset_response()
    body["data"]["asset"]["asset_prices"] = prices
    c = make_client(session, throttle, config, lambda r: httpx.Response(200, json=body))
    assert c.asset_files(UUID).price == expected
