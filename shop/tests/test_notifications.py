from smtplib import SMTPException
from unittest.mock import patch

from celery.exceptions import Retry
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import transaction
from django.test import TestCase, override_settings

from shop.models import Notification, Order
from shop.notifications import enqueue_notification, schedule_notification
from shop.tasks import NotificationDeliveryError, send_order_notification


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class NotificationTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("buyer", "buyer@example.com", "password")
        self.order = Order.objects.create(user=user, total="10.00")

    @patch("shop.tasks.send_order_notification.delay")
    def test_schedule_waits_for_commit_and_deduplicates(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            with transaction.atomic():
                schedule_notification(self.order, Notification.Kind.ORDER_CREATED)
                schedule_notification(self.order, Notification.Kind.ORDER_CREATED)
            delay.assert_not_called()
        delay.assert_called_once_with(Notification.objects.get().pk)

    @patch("shop.tasks.send_order_notification.delay")
    def test_rollback_does_not_queue_or_persist_notification(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    schedule_notification(self.order, Notification.Kind.PAID)
                    raise ValueError("rollback")
            except ValueError:
                pass
        self.assertFalse(Notification.objects.exists())
        delay.assert_not_called()

    def test_email_uses_order_owner_and_duplicate_task_does_not_resend(self):
        notification = Notification.objects.create(order=self.order, kind=Notification.Kind.PAID)
        send_order_notification.run(notification.pk)
        send_order_notification.run(notification.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["buyer@example.com"])
        self.assertIn(str(self.order.pk), mail.outbox[0].body)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.SENT)
        self.assertIsNotNone(notification.sent_at)

    @patch("shop.tasks.send_mail", side_effect=SMTPException("temporary"))
    def test_transient_failure_retries_with_bound(self, send_mail):
        notification = Notification.objects.create(order=self.order, kind=Notification.Kind.PAID)
        with patch.object(send_order_notification, "retry", side_effect=Retry()) as retry:
            with self.assertRaises(Retry):
                send_order_notification.run(notification.pk)
        self.assertEqual(retry.call_args.kwargs["countdown"], 60)
        self.assertEqual(send_order_notification.max_retries, 3)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.PENDING)
        send_order_notification.push_request(retries=3)
        try:
            with self.assertRaises(NotificationDeliveryError):
                send_order_notification.run(notification.pk)
        finally:
            send_order_notification.pop_request()
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.FAILED)

    @patch("shop.tasks.send_order_notification.delay", side_effect=OSError("broker unavailable"))
    def test_publish_failure_leaves_recoverable_pending_record(self, delay):
        notification = Notification.objects.create(order=self.order, kind=Notification.Kind.PAID)
        with self.assertLogs("shop.notifications", level="ERROR") as logs:
            enqueue_notification(notification.pk)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.PENDING)
        self.assertIn("action=dispatch_notifications", logs.output[0])
