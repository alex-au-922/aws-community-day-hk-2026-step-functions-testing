from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.parser import event_parser  # pyright: ignore[reportUnknownVariableType]
from aws_lambda_powertools.utilities.typing import LambdaContext
from boto3.session import Session
from pydantic import JsonValue

from src.lambdas.orders.client import OrderClient
from src.lambdas.orders.errors import OrderNotCancellable
from src.lambdas.orders.models import (
    CancelUnpaidOrder,
    ConfirmOrder,
    CreateOrder,
    GetOrder,
    ReleaseStock,
    Request,
    ReserveStock,
)
from src.lambdas.orders.settings import Settings

logger = Logger(service="orders")


@logger.inject_lambda_context
@event_parser(model=Request)  # pyright: ignore[reportUntypedFunctionDecorator]
def handle(event: Request, context: LambdaContext) -> dict[str, JsonValue]:
    """Run one validated order action and return the serialized order.

    A cancellation read accepts only confirmed or already cancelled orders.
    """

    settings = Settings.model_validate({})
    orders = OrderClient(
        session=Session(),
        table_name=settings.table_name,
        stock_table_name=settings.stock_table_name,
    )
    match event.root:
        case CreateOrder() as action:
            order = orders.create_order(
                order_id=action.order_id,
                sku=action.sku,
                quantity=action.quantity,
                amount_minor=action.amount_minor,
                currency=action.currency,
            )
        case GetOrder() as action:
            order = orders.get_order(order_id=action.order_id)
            if order.status not in {"CONFIRMED", "CANCELLED"}:
                raise OrderNotCancellable(
                    f"Order cannot be cancelled while {order.status}"
                )
        case ReserveStock() as action:
            order = orders.reserve_stock(order=action.order)
        case ConfirmOrder() as action:
            order = orders.confirm_order(
                order=action.order, payment_id=action.payment_id
            )
        case CancelUnpaidOrder() as action:
            order = orders.cancel_unpaid_order(order=action.order)
        case ReleaseStock() as action:
            order = orders.release_stock(order=action.order)
        case action:
            order = orders.record_refund(order=action.order, refund_id=action.refund_id)
    logger.info(
        "Order action completed", action=event.root.action, order_id=order.order_id
    )
    return order.model_dump(mode="json", exclude_none=True)
