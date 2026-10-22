import logging
from functools import partial

from django.db import transaction

from .models import Notification

logger = logging.getLogger(__name__)


def enqueue_notification(notification_id):
    from .tasks import send_order_notification

    try:
        send_order_notification.delay(notification_id)
    except Exception as exc:
        # Keep the durable pending record for dispatch_notifications to recover.
        logger.error(
            "notification_publish_failed notification_id=%s error_type=%s action=dispatch_notifications",
            notification_id,
            type(exc).__name__,
        )


def schedule_notification(order, kind):
    notification, created = Notification.objects.get_or_create(order=order, kind=kind)
    if created:
        transaction.on_commit(partial(enqueue_notification, notification.pk))
