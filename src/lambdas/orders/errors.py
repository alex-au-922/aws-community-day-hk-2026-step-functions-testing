class OrderConflict(Exception):
    """An order ID already belongs to different product or payment details."""

    pass


class OrderNotFound(Exception):
    """The requested order does not exist."""

    pass


class OrderNotCancellable(Exception):
    """The order has not reached a state that permits cancellation."""

    pass


class OutOfStock(Exception):
    """Available stock cannot satisfy the requested order quantity."""

    pass
