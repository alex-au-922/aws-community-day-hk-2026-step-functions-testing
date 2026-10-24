from typing import Annotated, TypeAlias

from pydantic import Field

OrderId: TypeAlias = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
]

# Amount in the currency's smallest unit: 12500 means HKD 125.00.
AmountInMinorUnits: TypeAlias = Annotated[int, Field(gt=0, le=99_999_999)]
CurrencyCode: TypeAlias = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

# Stripe's identifier returned by POST /v1/payment_intents.
PaymentId: TypeAlias = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^\S+$")
]

# An authorized payment method, collected before the order workflow starts.
PaymentMethodId: TypeAlias = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^pm_\S+$")
]

# Stripe's identifier returned by POST /v1/refunds.
RefundId: TypeAlias = Annotated[
    str, Field(min_length=1, max_length=256, pattern=r"^\S+$")
]
