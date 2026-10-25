"""Coordinate the existing order clients using checkpointed Python steps."""

import json

from aws_durable_execution_sdk_python import DurableContext, durable_execution
from aws_durable_execution_sdk_python.config import (
    Duration,
    StepConfig,
    StepSemantics,
    WaitForCallbackConfig,
)
from aws_durable_execution_sdk_python.retries import RetryPresets
from aws_durable_execution_sdk_python.types import WaitForCallbackContext
from boto3.session import Session
from pydantic import Field, JsonValue
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.lambdas.orders.client import OrderClient
from src.lambdas.orders.models import OrderDetails, OrderRecord
from src.lambdas.orders.settings import Settings as OrderSettings
from src.lambdas.payments.client import PaymentClient
from src.lambdas.payments.settings import Settings as PaymentSettings
from src.lambdas.payments.types import PaymentMethodId


class OrderInput(OrderDetails):
    """Accept the same order details and payment method as the ASL workflow."""

    payment_method_id: PaymentMethodId


class MerchantSettings(BaseSettings):
    """Route approval requests to the configured merchant queue."""

    model_config = SettingsConfigDict(env_prefix="MERCHANT_")

    queue_url: str = Field(min_length=1)


@durable_execution
def handle(
    event: dict[str, JsonValue], context: DurableContext
) -> dict[str, JsonValue]:
    """Wait for approval, reserve two units, charge once, then confirm.

    This first slice covers approval success. Explicit no-retry, at-most-once
    business mutation steps leave uncertain outcomes for reconciliation instead of repeating IO.
    """
    request = OrderInput.model_validate(event)
    session = Session()
    order_settings = OrderSettings.model_validate({})
    payment_settings = PaymentSettings.model_validate({})
    merchant = MerchantSettings.model_validate({})
    orders = OrderClient(
        session=session,
        table_name=order_settings.table_name,
        stock_table_name=order_settings.stock_table_name,
    )
    payments = PaymentClient(
        api_url=payment_settings.api_url,
        api_key=payment_settings.api_key,
        timeout_seconds=payment_settings.timeout_seconds,
    )
    once = StepConfig(
        retry_strategy=RetryPresets.none(),
        step_semantics=StepSemantics.AT_MOST_ONCE_PER_RETRY,
    )
    order = OrderRecord.model_validate(
        context.step(
            lambda _: orders.create_order(
                order_id=request.order_id,
                sku=request.sku,
                quantity=request.quantity,
                amount_minor=request.amount_minor,
                currency=request.currency,
            ).model_dump(mode="json", exclude_none=True),
            name="CreateOrder",
            config=once,
        )
    )
    if order.status != "PENDING":
        return order.model_dump(mode="json", exclude_none=True)

    def request_approval(callback_id: str, _: WaitForCallbackContext) -> None:
        session.client("sqs").send_message(  # pyright: ignore[reportUnknownMemberType]
            QueueUrl=merchant.queue_url,
            MessageBody=json.dumps(
                {**order.model_dump(mode="json"), "callback_id": callback_id}
            ),
        )

    context.wait_for_callback(
        request_approval,
        name="AwaitMerchantApproval",
        config=WaitForCallbackConfig(
            timeout=Duration.from_seconds(120),
            heartbeat_timeout=Duration.from_seconds(120),
            retry_strategy=RetryPresets.none(),
        ),
    )
    order = OrderRecord.model_validate(
        context.step(
            lambda _: orders.reserve_stock(order=order).model_dump(
                mode="json", exclude_none=True
            ),
            name="ReserveStock",
            config=once,
        )
    )
    payment_id = context.step(
        lambda _: (
            payments.charge(
                order_id=order.order_id,
                amount_minor=order.amount_minor,
                currency=order.currency,
                payment_method_id=request.payment_method_id,
            ).payment_id
        ),
        name="ChargePayment",
        config=once,
    )
    return context.step(
        lambda _: orders.confirm_order(order=order, payment_id=payment_id).model_dump(
            mode="json", exclude_none=True
        ),
        name="ConfirmOrder",
        config=once,
    )
