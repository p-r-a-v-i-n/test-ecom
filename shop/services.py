import logging
from decimal import Decimal
from uuid import UUID

import stripe
from django.conf import settings
from django.db import transaction
from rest_framework.exceptions import ValidationError

from .models import Notification, Order, OrderItem, Product, StripeEvent
from .notifications import schedule_notification

logger = logging.getLogger(__name__)
SUPPORTED_EVENTS = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "checkout.session.expired",
}


class CheckoutUnavailable(Exception):
    def __init__(self, order_id):
        self.order_id = order_id
        super().__init__("Payment provider unavailable")


@transaction.atomic
def create_pending_order(user, items):
    # Lock catalog rows while taking the immutable purchase snapshot.
    products = {
        product.pk: product
        for product in Product.objects.select_for_update().filter(
            pk__in=[item["product_id"] for item in items], is_active=True
        ).order_by("pk")
    }
    if len(products) != len(items):
        raise ValidationError({"items": "One or more products are unavailable."})
    total = sum(
        (products[item["product_id"]].price * item["quantity"] for item in items),
        Decimal("0.00"),
    )
    # This initial flow uses USD (two decimal places) and Stripe's USD limits.
    if total < Decimal("0.50") or total > Decimal("999999.99"):
        raise ValidationError({"items": "Order total must be between USD 0.50 and USD 999999.99."})
    order = Order.objects.create(user=user, total=total)
    OrderItem.objects.bulk_create([
        OrderItem(
            order=order,
            product=products[item["product_id"]],
            product_name=products[item["product_id"]].name,
            unit_price=products[item["product_id"]].price,
            quantity=item["quantity"],
        )
        for item in items
    ])
    schedule_notification(order, Notification.Kind.ORDER_CREATED)
    logger.info("order_created order_id=%s", order.pk)
    return order


def create_checkout(user, items):
    # Commit first, so an early webhook can find the order via Stripe metadata.
    order = create_pending_order(user, items)
    client = stripe.StripeClient(settings.STRIPE_SECRET_KEY, max_network_retries=2)
    try:
        session = client.v1.checkout.sessions.create(
            {
                "mode": "payment",
                "client_reference_id": str(order.pk),
                "metadata": {"order_id": str(order.pk)},
                "success_url": settings.STRIPE_SUCCESS_URL,
                "cancel_url": settings.STRIPE_CANCEL_URL,
                "customer_email": user.email,
                "line_items": [
                    {
                        "price_data": {
                            "currency": order.currency,
                            "unit_amount": int(item.unit_price * 100),
                            "product_data": {"name": item.product_name},
                        },
                        "quantity": item.quantity,
                    }
                    for item in order.items.all()
                ],
            },
            options={"idempotency_key": f"checkout-{order.pk}"},
        )
    except stripe.StripeError as exc:
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            if locked.status == Order.Status.PENDING and not locked.stripe_session_id:
                locked.status = Order.Status.CHECKOUT_FAILED
                locked.save(update_fields=["status", "updated_at"])
        logger.error("checkout_failed order_id=%s error_type=%s", order.pk, type(exc).__name__)
        raise CheckoutUnavailable(order.pk) from None

    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        # A verified webhook may already have associated this same session.
        if order.stripe_session_id and order.stripe_session_id != session.id:
            logger.error("checkout_association_conflict order_id=%s", order.pk)
            raise CheckoutUnavailable(order.pk)
        order.stripe_session_id = session.id
        order.checkout_url = session.url
        order.save(update_fields=["stripe_session_id", "checkout_url", "updated_at"])
    logger.info("checkout_created order_id=%s session_id=%s", order.pk, session.id)
    return order


@transaction.atomic
def apply_stripe_event(event):
    event_type = event["type"]
    if event_type not in SUPPORTED_EVENTS:
        return
    session = event["data"]["object"]
    if session.get("object") != "checkout.session" or session.get("mode") != "payment":
        return
    session_id = session["id"]
    order = Order.objects.select_for_update().filter(stripe_session_id=session_id).first()
    if order is None:
        # Covers a webhook arriving before the Session-create response, including
        # an ambiguous provider timeout. Never attach a second session to an order.
        try:
            order_id = UUID(session.get("metadata", {}).get("order_id", ""))
        except (ValueError, TypeError, AttributeError):
            return
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order and order.stripe_session_id not in (None, session_id):
            return
    if order is None:
        return
    if (
        session.get("client_reference_id") != str(order.pk)
        or session.get("metadata", {}).get("order_id") != str(order.pk)
        or session.get("currency") != order.currency
        or session.get("amount_total") != int(order.total * 100)
    ):
        logger.warning("payment_association_rejected order_id=%s event_id=%s", order.pk, event["id"])
        return
    intent_id = session.get("payment_intent") or None
    if order.payment_intent_id and intent_id and order.payment_intent_id != intent_id:
        logger.warning("payment_reference_rejected order_id=%s event_id=%s", order.pk, event["id"])
        return
    _, created = StripeEvent.objects.get_or_create(
        pk=event["id"],
        defaults={"event_type": event_type, "provider_created": event["created"], "order": order},
    )
    if not created:
        return
    order.stripe_session_id = session_id
    if intent_id:
        order.payment_intent_id = intent_id
    order.save(update_fields=["stripe_session_id", "payment_intent_id", "updated_at"])

    outcome = None
    if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        if session.get("payment_status") == "paid":
            outcome = Order.Status.PAID
    elif session.get("payment_status") != "paid":
        outcome = (
            Order.Status.FAILED
            if event_type == "checkout.session.async_payment_failed"
            else Order.Status.EXPIRED
        )
    if outcome is None or order.status == Order.Status.PAID:
        return
    # Confirmed success wins even when its event was delivered late. Other
    # outcomes cannot overwrite a newer outcome, or trigger duplicate emails.
    if outcome != Order.Status.PAID and event["created"] < order.last_payment_event_created:
        return
    previous = order.status
    order.status = outcome
    order.last_payment_event_created = max(order.last_payment_event_created, event["created"])
    order.save(update_fields=["status", "last_payment_event_created", "updated_at"])
    if previous != outcome:
        schedule_notification(order, outcome)
        logger.info(
            "payment_transition order_id=%s session_id=%s payment_id=%s event_id=%s status=%s",
            order.pk, session_id, order.payment_intent_id, event["id"], outcome,
        )
