# app/exceptions.py

class SwingAlgoError(Exception):
    """Base exception for all SwingAlgo failures."""
    pass

class ShoonyaAPIException(SwingAlgoError):
    """Raised for any underlying Shoonya API errors."""
    pass

class OrderPendingException(SwingAlgoError):
    """Raised when an order is already pending for a user-script."""
    pass

class PlaceOrderRetry(SwingAlgoError):
    """Used to trigger a retry on network/API glitches."""
    pass
