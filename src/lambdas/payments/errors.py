class PaymentRejected(RuntimeError):
    """Stripe definitively declined this payment without charging it."""


class PaymentUnavailable(RuntimeError):
    """The payment outcome requires reconciliation before another attempt."""
