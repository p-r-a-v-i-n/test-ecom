from smtplib import SMTPException
from unittest.mock import call, patch

from celery.exceptions import Retry
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
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
        for index, kind in enumerate(Notification.Kind.values, start=1):
            with self.subTest(kind=kind):
                notification = Notification.objects.create(order=self.order, kind=kind)
                send_order_notification.run(notification.pk)
                send_order_notification.run(notification.pk)
                self.assertEqual(len(mail.outbox), index)
                message = mail.outbox[-1]
                self.assertEqual(message.to, ["buyer@example.com"])
                self.assertIn(str(self.order.pk), message.subject)
                self.assertIn(str(self.order.pk), message.body)
                self.assertIn("USD 10.00", message.body)
                notification.refresh_from_db()
                self.assertEqual(notification.status, Notification.Status.SENT)
                self.assertIsNotNone(notification.sent_at)

    @patch("shop.tasks.send_order_notification.delay")
    def test_dispatch_notifications_only_requeues_pending(self, delay):
        pending = [
            Notification.objects.create(order=self.order, kind=Notification.Kind.ORDER_CREATED),
            Notification.objects.create(order=self.order, kind=Notification.Kind.PAID),
        ]
        Notification.objects.create(order=self.order, kind=Notification.Kind.FAILED, status=Notification.Status.FAILED)
        Notification.objects.create(order=self.order, kind=Notification.Kind.EXPIRED, status=Notification.Status.SENT)
        original = list(Notification.objects.order_by("pk").values())
        for _ in range(2):
            delay.reset_mock()
            call_command("dispatch_notifications")
            self.assertCountEqual(delay.call_args_list, [call(notification.pk) for notification in pending])
            self.assertEqual(list(Notification.objects.order_by("pk").values()), original)
        for notification in pending:
            send_order_notification.run(notification.pk)
        delay.reset_mock()
        call_command("dispatch_notifications")
        delay.assert_not_called()
        self.assertEqual(len(mail.outbox), 2)

    @patch("shop.tasks.send_mail", return_value=0)
    def test_zero_email_delivery_retries_without_marking_sent(self, send_mail):
        notification = Notification.objects.create(order=self.order, kind=Notification.Kind.PAID)
        with patch.object(send_order_notification, "retry", side_effect=Retry()) as retry:
            with self.assertRaises(Retry):
                send_order_notification.run(notification.pk)
        self.assertIsInstance(retry.call_args.kwargs["exc"], NotificationDeliveryError)
        self.assertEqual(retry.call_args.kwargs["countdown"], 60)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.PENDING)
        self.assertIsNone(notification.sent_at)

    @patch("shop.tasks.send_mail", side_effect=SMTPException("sentinel-smtp-credential"))
    def test_transient_failure_retries_with_bound(self, send_mail):
        notification = Notification.objects.create(order=self.order, kind=Notification.Kind.PAID)
        with patch.object(send_order_notification, "retry", side_effect=Retry()) as retry:
            with self.assertLogs("shop.tasks", level="ERROR") as logs:
                with self.assertRaises(Retry):
                    send_order_notification.run(notification.pk)
        self.assertIn(str(self.order.pk), " ".join(logs.output))
        self.assertIn(f"notification_id={notification.pk}", " ".join(logs.output))
        self.assertIn("SMTPException", " ".join(logs.output))
        self.assertNotIn("sentinel-smtp-credential", " ".join(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))
        self.assertEqual(retry.call_args.kwargs["countdown"], 60)
        self.assertNotIn("sentinel-smtp-credential", str(retry.call_args.kwargs["exc"]))
        self.assertEqual(send_order_notification.max_retries, 3)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.PENDING)
        send_order_notification.push_request(retries=3)
        try:
            with self.assertLogs("shop.tasks", level="ERROR") as exhausted:
                with self.assertRaises(NotificationDeliveryError) as failure:
                    send_order_notification.run(notification.pk)
        finally:
            send_order_notification.pop_request()
        self.assertNotIn("sentinel-smtp-credential", str(failure.exception))
        self.assertNotIn("sentinel-smtp-credential", " ".join(exhausted.output))
        self.assertIn(f"notification_id={notification.pk}", " ".join(exhausted.output))
        self.assertTrue(all(record.exc_info is None for record in exhausted.records))
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.FAILED)

    @patch("shop.tasks.send_order_notification.delay", side_effect=OSError("sentinel-broker-credential"))
    def test_publish_failure_leaves_recoverable_pending_record(self, delay):
        notification = Notification.objects.create(order=self.order, kind=Notification.Kind.PAID)
        with self.assertLogs("shop.notifications", level="ERROR") as logs:
            enqueue_notification(notification.pk)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.PENDING)
        self.assertIn("action=dispatch_notifications", logs.output[0])
        self.assertIn(f"notification_id={notification.pk}", " ".join(logs.output))
        self.assertIn("OSError", " ".join(logs.output))
        self.assertNotIn("sentinel-broker-credential", " ".join(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))
