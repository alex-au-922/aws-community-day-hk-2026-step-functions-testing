from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.parser import event_parser  # pyright: ignore[reportUnknownVariableType]
from aws_lambda_powertools.utilities.typing import LambdaContext
from pydantic import JsonValue

from src.lambdas.payments.client import PaymentClient
from src.lambdas.payments.models import Charge, Request
from src.lambdas.payments.settings import Settings

logger = Logger(service="payments")


@logger.inject_lambda_context
@event_parser(model=Request)  # pyright: ignore[reportUntypedFunctionDecorator]
def handle(event: Request, context: LambdaContext) -> dict[str, JsonValue]:
    """Dispatch a validated charge or refund request and return its identifier."""
    settings = Settings.model_validate({})
    payment = PaymentClient(
        api_url=settings.api_url,
        api_key=settings.api_key,
        timeout_seconds=settings.timeout_seconds,
    )
    match event.root:
        case Charge() as action:
            result = payment.charge(
                order_id=action.order_id,
                amount_minor=action.amount_minor,
                currency=action.currency,
                payment_method_id=action.payment_method_id,
            )
            logger.info(
                "Payment charged",
                order_id=action.order_id,
                payment_id=result.payment_id,
            )
        case action:
            result = payment.refund(
                order_id=action.order_id, payment_id=action.payment_id
            )
            logger.info(
                "Payment refunded", order_id=action.order_id, refund_id=result.refund_id
            )
    return result.model_dump(mode="json")
