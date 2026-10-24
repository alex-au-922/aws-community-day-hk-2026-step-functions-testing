from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, RootModel

from src.lambdas.payments.types import (
    AmountInMinorUnits,
    CurrencyCode,
    OrderId,
    PaymentId,
    PaymentMethodId,
)


class Charge(BaseModel):
    """Request a charge for an order, with amount in the currency's minor unit."""

    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal["charge"]
    order_id: OrderId
    amount_minor: AmountInMinorUnits
    currency: CurrencyCode
    payment_method_id: PaymentMethodId


class Refund(BaseModel):
    """Request a refund of the order's previously confirmed payment."""

    model_config = ConfigDict(strict=True, extra="forbid")

    action: Literal["refund"]
    order_id: OrderId
    payment_id: PaymentId


Action: TypeAlias = Charge | Refund


class Request(RootModel[Action]):
    """Select the validated payment request using its ``action`` field."""

    root: Action = Field(discriminator="action")
