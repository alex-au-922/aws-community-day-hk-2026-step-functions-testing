from typing import Literal

import httpx
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
)

from src.lambdas.payments.errors import (
    PaymentRejected,
    PaymentUnavailable,
)
from src.lambdas.payments.types import PaymentId, RefundId


class ChargeResponse(BaseModel):
    """Validate a succeeded Stripe PaymentIntent and expose its payment ID."""

    model_config = ConfigDict(strict=True)

    payment_id: PaymentId = Field(validation_alias="id")
    status: Literal["succeeded"] = Field(exclude=True)


class RefundResponse(BaseModel):
    """Validate a succeeded Stripe refund and expose its refund ID."""

    model_config = ConfigDict(strict=True)

    refund_id: RefundId = Field(validation_alias="id")
    status: Literal["succeeded"] = Field(exclude=True)


class PaymentClient(BaseModel):
    """Submit Stripe requests once and retain uncertain outcomes for reconciliation."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    api_url: AnyHttpUrl
    api_key: SecretStr
    timeout_seconds: float = Field(gt=0, le=10)

    def charge(
        self,
        *,
        order_id: str,
        amount_minor: int,
        currency: str,
        payment_method_id: str,
    ) -> ChargeResponse:
        """Charge an authorized payment method using ``charge:{order_id}``.

        Amounts use the currency's minor unit. Stripe handles idempotency keys
        within its retention window; localstripe does not emulate this feature.
        An unavailable response leaves the outcome uncertain and is not retried.
        """
        response = self._post(
            "payment_intents",
            {
                "amount": str(amount_minor),
                "currency": currency.lower(),
                "payment_method": payment_method_id,
                "confirm": "true",
                "off_session": "true",
                "metadata[order_id]": order_id,
            },
            f"charge:{order_id}",
        )
        try:
            return ChargeResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise PaymentUnavailable("Stripe has not confirmed payment") from error

    def refund(self, *, order_id: str, payment_id: str) -> RefundResponse:
        """Refund the payment using the stable ``refund:{order_id}`` key.

        An unavailable response leaves the refund outcome uncertain; the caller
        must reconcile it before making another attempt.
        """
        response = self._post(
            "refunds",
            {"payment_intent": payment_id, "metadata[order_id]": order_id},
            f"refund:{order_id}",
        )
        try:
            return RefundResponse.model_validate_json(response.content)
        except ValidationError as error:
            raise PaymentUnavailable("Stripe has not confirmed refund") from error

    def _post(
        self, path: str, payload: dict[str, str], idempotency_key: str
    ) -> httpx.Response:
        """Make one authenticated Stripe HTTP attempt and classify its response.

        HTTP 402 is a definite payment rejection. Network failures, unavailable
        responses and incomplete results remain uncertain. Other HTTP errors
        propagate, including idempotency conflicts that must not release stock.
        """
        try:
            response = httpx.post(
                f"{str(self.api_url).rstrip('/')}/v1/{path}",
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key.get_secret_value()}",
                    "Idempotency-Key": idempotency_key,
                    "Stripe-Version": "2026-09-30.endive",
                },
                timeout=self.timeout_seconds,
            )
        except httpx.RequestError as error:
            raise PaymentUnavailable("Stripe request failed") from error

        if response.status_code in {408, 429} or response.status_code >= 500:
            raise PaymentUnavailable(f"Stripe HTTP {response.status_code}")
        if response.status_code == 402:
            raise PaymentRejected("Stripe declined the payment")
        response.raise_for_status()
        if response.status_code not in {200, 201}:
            raise PaymentUnavailable("Stripe has not confirmed completion")
        return response
