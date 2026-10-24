from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, RootModel

from src.lambdas.orders.types import (
    AmountInMinorUnits,
    CurrencyCode,
    OrderId,
    PaymentId,
    Quantity,
    RefundId,
    Sku,
)


class OrderDetails(BaseModel):
    """Identify one order and its immutable product and payment details."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: OrderId
    sku: Sku
    quantity: Quantity
    amount_minor: AmountInMinorUnits
    currency: CurrencyCode


class OrderRecord(OrderDetails):
    """Represent stored order progress and any confirmed payment or refund."""

    status: Literal["PENDING", "RESERVED", "CONFIRMED", "CANCELLED"]
    payment_id: PaymentId | None = None
    refund_id: RefundId | None = None


class ConfirmedOrder(OrderDetails):
    """Require a confirmed payment before recording a refund."""

    status: Literal["CONFIRMED"]
    payment_id: PaymentId


class CreateOrder(OrderDetails):
    """Request creation of an order with fixed product and payment details."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    action: Literal["create_order"]


class GetOrder(BaseModel):
    """Request the stored order used to begin cancellation."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    action: Literal["get_order"]
    order_id: OrderId


class OrderAction(BaseModel):
    """Carry an order snapshot into a workflow state transition."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    order: OrderRecord


class ReserveStock(OrderAction):
    """Request a stock reservation for a pending order."""

    action: Literal["reserve_stock"]


class ConfirmOrder(OrderAction):
    """Request confirmation of a reserved order after successful payment."""

    action: Literal["confirm_order"]
    payment_id: PaymentId


class CancelUnpaidOrder(OrderAction):
    """Request cancellation before stock reservation or payment."""

    action: Literal["cancel_unpaid_order"]


class ReleaseStock(OrderAction):
    """Request cancellation and stock release after a definitive payment rejection."""

    action: Literal["release_stock"]


class RecordRefund(BaseModel):
    """Request cancellation and stock release after a confirmed refund."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    action: Literal["record_refund"]
    order: ConfirmedOrder
    refund_id: RefundId


Action: TypeAlias = (
    CreateOrder
    | GetOrder
    | ReserveStock
    | ConfirmOrder
    | CancelUnpaidOrder
    | ReleaseStock
    | RecordRefund
)


class Request(RootModel[Action]):
    """Validate an Orders Lambda event using its action discriminator."""

    root: Action = Field(discriminator="action")
