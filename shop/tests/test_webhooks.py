import hashlib
import hmac
import json
import time
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from shop.models import Notification, Order, StripeEvent


@override_settings(STRIPE_WEBHOOK_SECRET="whsec_test_only", SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
class WebhookTests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("buyer", "buyer@example.com", "password")
        self.order = Order.objects.create(user=self.user, total="24.68", stripe_session_id="cs_order")

    def event(self, event_id="evt_success", event_type="checkout.session.completed", created=100, paid=True):
        return {
            "id": event_id,
            "object": "event",
            "type": event_type,
            "created": created,
            "data": {"object": {
                "id": "cs_order", "object": "checkout.session", "mode": "payment",
                "metadata": {"order_id": str(self.order.pk)},
                "client_reference_id": str(self.order.pk),
                "currency": "usd", "amount_total": 2468,
                "payment_status": "paid" if paid else "unpaid",
                "payment_intent": "pi_order",
            }},
        }

    def deliver(self, event, signature=None):
        body = json.dumps(event).encode()
        if signature is None:
            timestamp = int(time.time())
            digest = hmac.new(b"whsec_test_only", str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()
            signature = f"t={timestamp},v1={digest}"
        return self.client.post("/api/stripe/webhook/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE=signature)

    def test_invalid_signature_and_missing_signature_are_rejected(self):
        self.assertEqual(self.deliver(self.event(), "invalid").status_code, 400)
        self.assertEqual(self.deliver(self.event(), "").status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertFalse(StripeEvent.objects.exists())

    @patch("shop.tasks.send_order_notification.delay")
    def test_paid_transition_deduplicates_deliveries_and_semantic_duplicates(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.deliver(self.event()).status_code, 200)
            delay.assert_not_called()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.deliver(self.event()).status_code, 200)
            self.assertEqual(self.deliver(self.event("evt_also_paid")).status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(self.order.payment_intent_id, "pi_order")
        self.assertEqual(StripeEvent.objects.count(), 2)
        self.assertEqual(Notification.objects.filter(order=self.order, kind="paid").count(), 1)
        delay.assert_called_once()

    @patch("shop.tasks.send_order_notification.delay")
    def test_failure_then_late_success_then_older_and_newer_failure(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            failed = self.event("evt_failed", "checkout.session.async_payment_failed", 200, paid=False)
            self.assertEqual(self.deliver(failed).status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.FAILED)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.deliver(self.event("evt_failed_again", "checkout.session.async_payment_failed", 201, paid=False)).status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.FAILED)
        self.assertEqual(self.order.last_payment_event_created, 201)
        self.assertEqual(StripeEvent.objects.count(), 2)
        self.assertEqual(Notification.objects.filter(order=self.order, kind=Notification.Kind.FAILED).count(), 1)
        delay.assert_called_once()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.deliver(self.event("evt_paid", "checkout.session.async_payment_succeeded", 100)).status_code, 200)
            self.deliver(self.event("evt_old_failure", "checkout.session.async_payment_failed", 50, paid=False))
            self.deliver(self.event("evt_new_failure", "checkout.session.async_payment_failed", 300, paid=False))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)
        self.assertEqual(delay.call_count, 2)

    @patch("shop.tasks.send_order_notification.delay")
    def test_unpaid_completion_waits_for_async_outcome(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.deliver(self.event(paid=False)).status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        delay.assert_not_called()

    @patch("shop.tasks.send_order_notification.delay")
    def test_expiry_and_older_failure(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.deliver(self.event("evt_expired", "checkout.session.expired", 200, paid=False)).status_code, 200)
            self.assertEqual(self.deliver(self.event("evt_expired_again", "checkout.session.expired", 201, paid=False)).status_code, 200)
            self.assertEqual(self.deliver(self.event("evt_older", "checkout.session.async_payment_failed", 100, paid=False)).status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.EXPIRED)
        self.assertEqual(self.order.last_payment_event_created, 201)
        self.assertEqual(StripeEvent.objects.count(), 3)
        self.assertEqual(Notification.objects.filter(order=self.order, kind=Notification.Kind.EXPIRED).count(), 1)
        delay.assert_called_once()

    @patch("shop.tasks.send_order_notification.delay")
    def test_signed_malformed_events_are_rejected_without_side_effects(self, delay):
        missing_data = self.event("evt_missing_data")
        del missing_data["data"]
        null_data = self.event("evt_null_data")
        null_data["data"] = None
        missing_session_id = self.event("evt_missing_session_id")
        del missing_session_id["data"]["object"]["id"]
        missing_created = self.event("evt_missing_created")
        del missing_created["created"]
        invalid_created = self.event("evt_invalid_created", created="not-a-timestamp")
        original = Order.objects.values().get(pk=self.order.pk)
        for event in [missing_data, null_data, missing_session_id, missing_created, invalid_created]:
            with self.subTest(event=event["id"]):
                with self.captureOnCommitCallbacks(execute=True):
                    self.assertEqual(self.deliver(event).status_code, 400)
                self.assertEqual(Order.objects.values().get(pk=self.order.pk), original)
                self.assertFalse(StripeEvent.objects.exists())
                self.assertFalse(Notification.objects.exists())
                delay.assert_not_called()

    @patch("shop.tasks.send_order_notification.delay")
    def test_webhook_ignored_cases_preserve_orders(self, delay):
        self.order.payment_intent_id = "pi_order"
        self.order.save()
        Order.objects.create(user=self.user, total="24.68", stripe_session_id="cs_other_order")
        cases = [
            ("non_checkout_object", {"object": "payment_intent"}),
            ("non_payment_mode", {"mode": "subscription"}),
            ("invalid_metadata_uuid", {"id": "cs_unknown", "metadata": {"order_id": "invalid-uuid"}}),
            ("null_metadata_uuid", {"id": "cs_unknown", "metadata": {"order_id": None}}),
            ("missing_metadata_uuid", {"id": "cs_unknown", "metadata": {}}),
            ("nonexistent_order", {"id": "cs_unknown", "metadata": {"order_id": str(uuid4())}}),
            ("conflicting_payment_intent", {"payment_intent": "pi_other"}),
            ("mismatched_metadata", {"metadata": {"order_id": str(uuid4())}}),
        ]
        original = list(Order.objects.order_by("pk").values())
        for label, updates in cases:
            with self.subTest(case=label):
                event = self.event(f"evt_{label}")
                event["data"]["object"].update(updates)
                with self.captureOnCommitCallbacks(execute=True):
                    self.assertEqual(self.deliver(event).status_code, 200)
                self.assertEqual(list(Order.objects.order_by("pk").values()), original)
                self.assertFalse(StripeEvent.objects.exists())
                self.assertFalse(Notification.objects.exists())
                delay.assert_not_called()

    @patch("shop.tasks.send_order_notification.delay")
    def test_unrelated_and_mismatched_sessions_are_acknowledged_without_transition(self, delay):
        irrelevant = self.event(event_type="customer.created")
        bad_reference = self.event("evt_other_session")
        bad_reference["data"]["object"]["id"] = "cs_other"
        wrong_total = self.event("evt_wrong_total")
        wrong_total["data"]["object"]["amount_total"] = 1
        wrong_currency = self.event("evt_wrong_currency")
        wrong_currency["data"]["object"]["currency"] = "eur"
        other_order = Order.objects.create(user=self.user, total="24.68", stripe_session_id="cs_other_order")
        wrong_owner = self.event("evt_wrong_order")
        wrong_owner["data"]["object"]["client_reference_id"] = str(other_order.pk)
        with self.captureOnCommitCallbacks(execute=True):
            for event in [irrelevant, bad_reference, wrong_total, wrong_currency, wrong_owner]:
                self.assertEqual(self.deliver(event).status_code, 200)
        self.order.refresh_from_db()
        other_order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)
        self.assertEqual(other_order.status, Order.Status.PENDING)
        self.assertFalse(StripeEvent.objects.exists())
        delay.assert_not_called()

    @patch("shop.tasks.send_order_notification.delay")
    def test_early_webhook_recovers_session_association_after_provider_timeout(self, delay):
        self.order.stripe_session_id = None
        self.order.status = Order.Status.CHECKOUT_FAILED
        self.order.save()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.deliver(self.event()).status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.stripe_session_id, "cs_order")
        self.assertEqual(self.order.status, Order.Status.PAID)
        delay.assert_called_once()
