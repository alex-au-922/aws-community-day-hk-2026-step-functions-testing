from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from boto3.session import Session
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.lambdas.orders.errors import OrderConflict, OrderNotFound, OutOfStock
from src.lambdas.orders.models import ConfirmedOrder, OrderDetails, OrderRecord
from src.lambdas.orders.types import (
    AmountInMinorUnits,
    CurrencyCode,
    OrderId,
    PaymentId,
    Quantity,
    RefundId,
    Sku,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient
    from mypy_boto3_dynamodb.type_defs import AttributeValueTypeDef

ORDER_NAMES = {
    "#status": "status",
    "#sku": "sku",
    "#quantity": "quantity",
    "#amount": "amount_minor",
    "#currency": "currency",
}
ORDER_MATCH = (
    "#sku = :sku AND #quantity = :quantity "
    "AND #amount = :amount AND #currency = :currency"
)


def _values(values: Mapping[str, str | int]) -> dict[str, "AttributeValueTypeDef"]:
    """Encode Python keys and expression values for the low-level DynamoDB API."""
    serializer = TypeSerializer()
    return {name: serializer.serialize(value) for name, value in values.items()}


def _record(item: Mapping[str, object]) -> OrderRecord:
    """Decode a DynamoDB item and validate it as a stored order."""
    deserializer = TypeDeserializer()
    return OrderRecord.model_validate(
        {
            name: deserializer.deserialize(cast("AttributeValueTypeDef", value))
            for name, value in item.items()
        }
    )


def _order_values(order: OrderDetails) -> dict[str, str | int]:
    """Bind immutable order fields used to guard each state change."""
    return {
        ":sku": order.sku,
        ":quantity": order.quantity,
        ":amount": order.amount_minor,
        ":currency": order.currency,
    }


class CancellationReason(BaseModel):
    """One transaction item's failure code and optional stored-item snapshot."""

    code: str = Field(alias="Code")
    item: dict[str, object] = Field(default_factory=dict, alias="Item")

    def matches(self, result: OrderRecord) -> bool:
        """Check whether a failed condition returned the exact intended result.

        A match identifies an already committed operation without moving stock again.
        """
        return (
            self.code == "ConditionalCheckFailed"
            and bool(self.item)
            and _record(self.item) == result
        )


class TransactionFailure(BaseModel):
    """Transaction cancellation details, ordered as the order item then stock item."""

    reasons: tuple[CancellationReason, CancellationReason] = Field(
        alias="CancellationReasons"
    )

    @classmethod
    def from_error(cls, error: ClientError) -> "TransactionFailure":
        """Parse item-level reasons, preserving the SDK error if parsing fails."""
        try:
            return cls.model_validate(error.response)
        except ValidationError:
            # Some SDK errors omit item-level details; preserve the original error.
            raise error from None


class OrderClient(BaseModel):
    """Manage orders and stock through an injected AWS session and two tables.

    Each public method performs one SDK operation. Stock changes and their order
    transitions commit together; repeated committed results do not move stock twice.
    """

    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, arbitrary_types_allowed=True
    )

    session: Session
    table_name: str = Field(min_length=1)
    stock_table_name: str = Field(min_length=1)

    @property
    def client(self) -> "DynamoDBClient":
        """Create a DynamoDB SDK client using the injected session."""
        return self.session.client("dynamodb")  # pyright: ignore[reportUnknownMemberType]

    def create_order(
        self,
        *,
        order_id: OrderId,
        sku: Sku,
        quantity: Quantity,
        amount_minor: AmountInMinorUnits,
        currency: CurrencyCode,
    ) -> OrderRecord:
        """Create a PENDING order or return its existing progress.

        Reusing an order ID requires identical SKU, quantity, amount, and currency;
        different details raise OrderConflict. Existing progress is never reset.
        """
        client = self.client
        try:
            response = client.update_item(
                TableName=self.table_name,
                Key=_values({"order_id": order_id}),
                UpdateExpression=(
                    "SET #sku = if_not_exists(#sku, :sku), "
                    "#quantity = if_not_exists(#quantity, :quantity), "
                    "#amount = if_not_exists(#amount, :amount), "
                    "#currency = if_not_exists(#currency, :currency), "
                    "#status = if_not_exists(#status, :pending)"
                ),
                ConditionExpression=f"attribute_not_exists(order_id) OR ({ORDER_MATCH})",
                ExpressionAttributeNames=ORDER_NAMES,
                ExpressionAttributeValues=_values(
                    {
                        ":sku": sku,
                        ":quantity": quantity,
                        ":amount": amount_minor,
                        ":currency": currency,
                        ":pending": "PENDING",
                    }
                ),
                ReturnValues="ALL_NEW",
            )
        except client.exceptions.ConditionalCheckFailedException as error:
            raise OrderConflict(
                "Order already exists with different items or payment"
            ) from error
        return _record(response.get("Attributes", {}))

    def get_order(self, *, order_id: OrderId) -> OrderRecord:
        """Read current order progress consistently; raise OrderNotFound if absent."""
        response = self.client.get_item(
            TableName=self.table_name,
            Key=_values({"order_id": order_id}),
            ConsistentRead=True,
        )
        item = response.get("Item", {})
        if not item:
            raise OrderNotFound("Order does not exist")
        return _record(item)

    def reserve_stock(self, *, order: OrderRecord) -> OrderRecord:
        """Atomically reserve quantity and change PENDING to RESERVED.

        Insufficient stock raises OutOfStock. Replaying the same committed
        reservation returns RESERVED without subtracting stock again.
        """
        return self._update_order_and_stock(
            target=order.model_copy(
                update={"status": "RESERVED", "payment_id": None, "refund_id": None}
            ),
            update_expression="SET #status = :reserved",
            condition_expression="#status = :pending",
            values={":pending": "PENDING", ":reserved": "RESERVED"},
            stock_change=-order.quantity,
        )

    def confirm_order(
        self, *, order: OrderRecord, payment_id: PaymentId
    ) -> OrderRecord:
        """Record a successful charge as CONFIRMED, allowing same-payment replay.

        This records the provider's result; it does not charge the customer.
        A cancelled order cannot be reopened by a delayed confirmation.
        """
        response = self.client.update_item(
            TableName=self.table_name,
            Key=_values({"order_id": order.order_id}),
            UpdateExpression="SET #status = :confirmed, payment_id = :payment",
            ConditionExpression=(
                f"({ORDER_MATCH}) AND "
                "(#status = :reserved OR "
                "(#status = :confirmed AND payment_id = :payment))"
            ),
            ExpressionAttributeNames=ORDER_NAMES,
            ExpressionAttributeValues=_values(
                {
                    **_order_values(order),
                    ":reserved": "RESERVED",
                    ":confirmed": "CONFIRMED",
                    ":payment": payment_id,
                }
            ),
            ReturnValues="ALL_NEW",
        )
        return _record(response.get("Attributes", {}))

    def cancel_unpaid_order(self, *, order: OrderRecord) -> OrderRecord:
        """Cancel a PENDING order whose stock was never reserved.

        The same unpaid CANCELLED result is safe to repeat. This operation does
        not release stock or cancel a reservation whose payment outcome is unknown.
        """
        response = self.client.update_item(
            TableName=self.table_name,
            Key=_values({"order_id": order.order_id}),
            UpdateExpression="SET #status = :cancelled",
            ConditionExpression=(
                f"({ORDER_MATCH}) AND "
                "#status IN (:pending, :cancelled) AND attribute_not_exists(payment_id)"
            ),
            ExpressionAttributeNames=ORDER_NAMES,
            ExpressionAttributeValues=_values(
                {
                    **_order_values(order),
                    ":pending": "PENDING",
                    ":cancelled": "CANCELLED",
                }
            ),
            ReturnValues="ALL_NEW",
        )
        return _record(response.get("Attributes", {}))

    def release_stock(self, *, order: OrderRecord) -> OrderRecord:
        """Cancel a RESERVED order and restore stock after definitive charge rejection.

        The workflow must establish rejection before calling this operation.
        Cancellation and stock restoration commit together and are safe to replay.
        """
        return self._update_order_and_stock(
            target=order.model_copy(
                update={"status": "CANCELLED", "payment_id": None, "refund_id": None}
            ),
            update_expression="SET #status = :cancelled",
            condition_expression="#status = :reserved",
            values={":reserved": "RESERVED", ":cancelled": "CANCELLED"},
            stock_change=order.quantity,
        )

    def record_refund(
        self, *, order: ConfirmedOrder, refund_id: RefundId
    ) -> OrderRecord:
        """Record a successful refund, cancel the order, and restore stock atomically.

        The input is the original CONFIRMED order and provider refund ID. This
        records the refund rather than issuing it; replay does not restore stock twice.
        """
        return self._update_order_and_stock(
            target=OrderRecord.model_validate(
                {**order.model_dump(), "status": "CANCELLED", "refund_id": refund_id}
            ),
            update_expression="SET #status = :cancelled, refund_id = :refund",
            condition_expression="#status = :confirmed AND payment_id = :payment",
            values={
                ":confirmed": "CONFIRMED",
                ":payment": order.payment_id,
                ":cancelled": "CANCELLED",
                ":refund": refund_id,
            },
            stock_change=order.quantity,
        )

    def _update_order_and_stock(
        self,
        *,
        target: OrderRecord,
        update_expression: str,
        condition_expression: str,
        values: Mapping[str, str | int],
        stock_change: int,
    ) -> OrderRecord:
        """Commit the supplied order update and stock delta in one transaction.

        A negative delta requires enough stock; a positive delta requires an
        existing stock row. The order condition also checks immutable fields.
        An exact stored target is accepted as replay before classifying a new
        reservation as OutOfStock; all other transaction failures propagate.
        """
        stock_condition = "attribute_exists(#available)"
        stock_values = {":change": stock_change}
        if stock_change < 0:
            stock_condition = "#available >= :quantity"
            stock_values[":quantity"] = -stock_change
        client = self.client
        try:
            client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": _values({"order_id": target.order_id}),
                            "UpdateExpression": update_expression,
                            "ConditionExpression": f"({condition_expression}) AND {ORDER_MATCH}",
                            "ExpressionAttributeNames": ORDER_NAMES,
                            "ExpressionAttributeValues": _values(
                                {
                                    **_order_values(target),
                                    **values,
                                }
                            ),
                            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.stock_table_name,
                            "Key": _values({"sku": target.sku}),
                            "UpdateExpression": "SET #available = #available + :change",
                            "ConditionExpression": stock_condition,
                            "ExpressionAttributeNames": {"#available": "available"},
                            "ExpressionAttributeValues": _values(stock_values),
                        }
                    },
                ]
            )
        except client.exceptions.TransactionCanceledException as error:
            failure = TransactionFailure.from_error(error)
            order_reason, stock_reason = failure.reasons
            if order_reason.matches(target):
                return target
            if (
                stock_change < 0
                and order_reason.code == "None"
                and stock_reason.code == "ConditionalCheckFailed"
            ):
                raise OutOfStock("Not enough stock for this order") from error
            raise
        return target
