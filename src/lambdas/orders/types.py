from typing import Annotated, TypeAlias

from pydantic import Field

OrderId: TypeAlias = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]

# The product's stock-keeping unit, shared by the order and stock records.
Sku: TypeAlias = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]
Quantity: TypeAlias = Annotated[int, Field(ge=1, le=999)]

# Amount in the currency's smallest unit: 12500 means HKD 125.00.
AmountInMinorUnits: TypeAlias = Annotated[int, Field(gt=0, le=99_999_999)]
CurrencyCode: TypeAlias = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

# Payment provider's identifier returned by POST /v1/payment_intents.
PaymentId: TypeAlias = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^\S+$")
]

# Payment provider's identifier returned by POST /v1/refunds.
RefundId: TypeAlias = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^\S+$")
]
