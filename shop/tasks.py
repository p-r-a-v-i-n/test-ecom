import logging
from smtplib import SMTPException

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from .models import Notification

logger = logging.getLogger(__name__)
MESSAGES = {
    Notification.Kind.ORDER_CREATED: ("Order created", "Your order has been created and is awaiting payment."),
    Notification.Kind.PAID: ("Payment received", "Your order payment has been confirmed."),
    Notification.Kind.FAILED: ("Payment failed", "Your order payment failed."),
    Notification.Kind.EXPIRED: ("Checkout expired", "Your checkout expired without a confirmed payment."),
}


class NotificationDeliveryError(Exception):
    """Safe error text for Celery's own retry/failure logging."""


@shared_task(bind=True, max_retries=3, ignore_result=True)
def send_order_notification(self, notification_id):
    try:
        with transaction.atomic():
            notification = Notification.objects.select_for_update().get(pk=notification_id)
            if notification.status != Notification.Status.PENDING:
                return
            order = notification.order
            subject, message = MESSAGES[notification.kind]
            delivered = send_mail(
                f"{subject}: {order.pk}",
                f"{message}\nOrder: {order.pk}\nTotal: {order.currency.upper()} {order.total}",
                settings.DEFAULT_FROM_EMAIL,
                [order.user.email],
                fail_silently=False,
            )
            if delivered != 1:
                raise OSError("Email backend did not accept the message")
            notification.status = Notification.Status.SENT
            notification.sent_at = timezone.now()
            notification.save(update_fields=["status", "sent_at"])
            logger.info("notification_sent order_id=%s notification_id=%s kind=%s", order.pk, notification.pk, notification.kind)
    except (OSError, SMTPException) as exc:
        logger.error(
            "notification_delivery_failed order_id=%s notification_id=%s attempt=%s error_type=%s",
            order.pk, notification_id, self.request.retries + 1, type(exc).__name__,
        )
        if self.request.retries >= self.max_retries:
            Notification.objects.filter(pk=notification_id, status=Notification.Status.PENDING).update(status=Notification.Status.FAILED)
            logger.error("notification_retries_exhausted notification_id=%s action=inspect_email_configuration", notification_id)
            raise NotificationDeliveryError("Email delivery retries exhausted") from None
        raise self.retry(
            exc=NotificationDeliveryError("Email delivery temporarily unavailable"),
            countdown=min(60 * (2 ** self.request.retries), 300),
        ) from None
