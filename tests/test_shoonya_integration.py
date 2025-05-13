import pytest
from app.shoonya_integration import ShoonyaAPIException, get_quotes, place_order

class BrokenQuotesAPI:
    def get_quotes(self, **kwargs):
        raise RuntimeError("network down")

def test_get_quotes_raises():
    with pytest.raises(ShoonyaAPIException) as exc:
        get_quotes(BrokenQuotesAPI(), token="XYZ")
    assert "get_quotes failed" in str(exc.value)

class BrokenOrderAPI:
    def place_order(self, **kwargs):
        raise ValueError("bad params")

def test_place_order_raises():
    with pytest.raises(ShoonyaAPIException) as exc:
        place_order(BrokenOrderAPI(), foo="bar")
    assert "place_order failed" in str(exc.value)
