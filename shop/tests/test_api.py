from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import stripe
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from shop.models import Notification, Order, Product, StripeEvent
from shop.services import apply_stripe_event

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
class AuthenticationTests(APITestCase):
    def test_registration_hashing_and_password_validation(self):
        rejected = self.client.post("/api/auth/register/", {
            "username": "reader", "email": "reader@example.com", "password": "123",
        }, format="json")
        self.assertEqual(rejected.status_code, 400)
        password = "Sufficiently-Complex-Password-873!"
        overlength = self.client.post("/api/auth/register/", {
            "username": "reader", "email": "a" * 64 + "@" + ".".join(["b" * 63] * 3), "password": password,
        }, format="json")
        self.assertEqual(overlength.status_code, 400)
        self.assertIn("email", overlength.data)
        self.assertFalse(User.objects.exists())
        response = self.client.post("/api/auth/register/", {
            "username": "reader", "email": "reader@example.com", "password": password,
        }, format="json")
        self.assertEqual(response.status_code, 201)
        user = User.objects.get(username="reader")
        self.assertNotEqual(user.password, password)
        self.assertTrue(user.check_password(password))
        self.assertNotIn("password", response.data)

    def test_token_issue_refresh_and_rejection(self):
        User.objects.create_user("reader", "reader@example.com", "Strong-Password-937!")
        denied = self.client.post("/api/auth/token/", {"username": "reader", "password": "incorrect"}, format="json")
        self.assertEqual(denied.status_code, 401)
        issued = self.client.post("/api/auth/token/", {"username": "reader", "password": "Strong-Password-937!"}, format="json")
        self.assertEqual(issued.status_code, 200)
        refreshed = self.client.post("/api/auth/token/refresh/", {"refresh": issued.data["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(self.client.get("/api/orders/").status_code, 401)
        self.assertEqual(self.client.post("/api/checkout/", {}, format="json").status_code, 401)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refreshed.data['access']}")
        self.assertEqual(self.client.get("/api/orders/").status_code, 200)

    def test_orders_are_owner_scoped(self):
        owner = User.objects.create_user("owner", "owner@example.com", "password")
        other = User.objects.create_user("other", "other@example.com", "password")
        order = Order.objects.create(user=owner, total="10.00")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f"/api/orders/{order.pk}/").status_code, 404)
        self.assertEqual(self.client.get("/api/orders/").data["results"], [])
        self.client.force_authenticate(owner)
        self.assertEqual(self.client.get(f"/api/orders/{order.pk}/").status_code, 200)


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
class CheckoutTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("buyer", "buyer@example.com", "password")
        self.product = Product.objects.create(name="Book", category="book", price="12.34")
        self.client.force_authenticate(self.user)

    def checkout(self, items):
        return self.client.post("/api/checkout/", {"items": items, "total": "0.01"}, format="json")

    def test_catalog_supports_all_categories_and_hides_inactive_products(self):
        for category in ["table", "laptop", "phone", "accessory", "other"]:
            Product.objects.create(name=category, category=category, price="10.00")
        hidden = Product.objects.create(name="Hidden", category="book", price="1.00", is_active=False)
        self.client.force_authenticate(None)
        response = self.client.get("/api/products/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["results"]), 6)
        self.assertEqual(self.client.get(f"/api/products/{self.product.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/products/{hidden.pk}/").status_code, 404)

    @patch("shop.services.stripe.StripeClient")
    @patch("shop.tasks.send_order_notification.delay")
    def test_checkout_uses_snapshots_and_server_prices(self, delay, stripe_client):
        stripe_client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(
            id="cs_created", url="https://checkout.stripe.com/example",
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.checkout([{"product_id": self.product.pk, "quantity": 3, "price": "0.01"}])
            delay.assert_not_called()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["checkout_url"], "https://checkout.stripe.com/example")
        order = Order.objects.get(pk=response.data["order_id"])
        self.assertEqual(order.total, Decimal("37.02"))
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.stripe_session_id, "cs_created")
        params = stripe_client.return_value.v1.checkout.sessions.create.call_args.args[0]
        self.assertEqual(params["line_items"][0]["price_data"]["unit_amount"], 1234)
        self.assertEqual(params["line_items"][0]["quantity"], 3)
        self.assertEqual(params["metadata"]["order_id"], str(order.pk))
        self.product.name = "Changed"
        self.product.price = Decimal("99.00")
        self.product.save()
        item = order.items.get()
        self.assertEqual((item.product_name, item.unit_price, item.quantity), ("Book", Decimal("12.34"), 3))
        delay.assert_called_once_with(Notification.objects.get(order=order).pk)

    @patch("shop.services.stripe.StripeClient")
    def test_invalid_selections_never_call_stripe_or_create_orders(self, stripe_client):
        selections = [
            [],
            [{"product_id": self.product.pk, "quantity": 0}],
            [{"product_id": self.product.pk, "quantity": -1}],
            [{"product_id": self.product.pk, "quantity": 1.5}],
            [{"product_id": self.product.pk, "quantity": 100}],
            [{"product_id": 999999, "quantity": 1}],
            [{"product_id": self.product.pk, "quantity": 1}] * 2,
        ]
        for items in selections:
            with self.subTest(items=items):
                self.assertEqual(self.checkout(items).status_code, 400)
        self.product.is_active = False
        self.product.save()
        self.assertEqual(self.checkout([{"product_id": self.product.pk, "quantity": 1}]).status_code, 400)
        self.assertFalse(Order.objects.exists())
        stripe_client.assert_not_called()

    @patch("shop.services.stripe.StripeClient")
    @patch("shop.tasks.send_order_notification.delay")
    def test_checkout_enforces_cart_and_total_limits(self, delay, stripe_client):
        for price in ["0.49", "1000000.00"]:
            with self.subTest(rejected_total=price):
                self.product.price = Decimal(price)
                self.product.save()
                self.assertEqual(self.checkout([{"product_id": self.product.pk, "quantity": 1}]).status_code, 400)
                self.assertFalse(Order.objects.exists())
                self.assertFalse(Notification.objects.exists())
                stripe_client.assert_not_called()

        products = [self.product] + [
            Product.objects.create(name=f"Accessory {index}", category="accessory", price="0.01")
            for index in range(100)
        ]
        oversized = [{"product_id": product.pk, "quantity": 1} for product in products]
        self.assertEqual(self.checkout(oversized).status_code, 400)
        self.assertFalse(Order.objects.exists())
        self.assertFalse(Notification.objects.exists())
        stripe_client.assert_not_called()
        delay.assert_not_called()

        for index, price in enumerate(["0.50", "999999.99"]):
            with self.subTest(accepted_total=price):
                self.product.price = Decimal(price)
                self.product.save()
                stripe_client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(
                    id=f"cs_boundary_{index}", url="https://checkout.stripe.com/example",
                )
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.checkout([{"product_id": self.product.pk, "quantity": 1}])
                self.assertEqual(response.status_code, 201)
                self.assertEqual(Order.objects.get(pk=response.data["order_id"]).total, Decimal(price))

        self.product.price = Decimal("0.01")
        self.product.save()
        stripe_client.return_value.v1.checkout.sessions.create.return_value = SimpleNamespace(
            id="cs_full_cart", url="https://checkout.stripe.com/example",
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.checkout(oversized[:100])
        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(pk=response.data["order_id"])
        self.assertEqual(order.items.count(), 100)
        self.assertEqual(order.total, Decimal("1.00"))

    def payment_event(self, params, outcome):
        event_types = {
            Order.Status.PAID: "checkout.session.async_payment_succeeded",
            Order.Status.FAILED: "checkout.session.async_payment_failed",
            Order.Status.EXPIRED: "checkout.session.expired",
        }
        return {
            "id": f"evt_{params['client_reference_id']}", "type": event_types[outcome], "created": 100,
            "data": {"object": {
                "id": f"cs_{params['client_reference_id']}", "object": "checkout.session", "mode": "payment",
                "metadata": params["metadata"], "client_reference_id": params["client_reference_id"],
                "currency": "usd", "amount_total": 1234,
                "payment_status": "paid" if outcome == Order.Status.PAID else "unpaid",
                "payment_intent": f"pi_{params['client_reference_id']}",
            }},
        }

    @patch("shop.services.stripe.StripeClient")
    @patch("shop.tasks.send_order_notification.delay")
    def test_checkout_preserves_webhook_outcome_during_provider_response(self, delay, stripe_client):
        for outcome in [Order.Status.PAID, Order.Status.FAILED, Order.Status.EXPIRED]:
            for timeout in [False, True]:
                with self.subTest(outcome=outcome, timeout=timeout):
                    def provider_response(params, **kwargs):
                        event = self.payment_event(params, outcome)
                        apply_stripe_event(event)
                        if timeout:
                            raise stripe.APIConnectionError("provider timeout")
                        return SimpleNamespace(id=event["data"]["object"]["id"], url="https://checkout.stripe.com/example")

                    stripe_client.return_value.v1.checkout.sessions.create.side_effect = provider_response
                    delay.reset_mock()
                    with self.captureOnCommitCallbacks(execute=True):
                        response = self.checkout([{"product_id": self.product.pk, "quantity": 1}])
                        delay.assert_not_called()
                    self.assertEqual(response.status_code, 503 if timeout else 201)
                    order = Order.objects.get(pk=response.data["order_id"])
                    self.assertEqual(order.status, outcome)
                    self.assertEqual(order.stripe_session_id, f"cs_{order.pk}")
                    self.assertEqual(order.payment_intent_id, f"pi_{order.pk}")
                    self.assertEqual(order.last_payment_event_created, 100)
                    self.assertEqual(order.checkout_url, "" if timeout else "https://checkout.stripe.com/example")
                    self.assertEqual(StripeEvent.objects.filter(order=order).count(), 1)
                    self.assertCountEqual(order.notifications.values_list("kind", flat=True), [Notification.Kind.ORDER_CREATED, outcome])
                    self.assertEqual(delay.call_count, 2)

    @patch("shop.services.stripe.StripeClient")
    @patch("shop.tasks.send_order_notification.delay")
    def test_checkout_rejects_conflicting_session_response(self, delay, stripe_client):
        def provider_response(params, **kwargs):
            apply_stripe_event(self.payment_event(params, Order.Status.PAID))
            return SimpleNamespace(id="cs_conflicting", url="https://checkout.stripe.com/conflicting")

        stripe_client.return_value.v1.checkout.sessions.create.side_effect = provider_response
        with self.captureOnCommitCallbacks(execute=True):
            response = self.checkout([{"product_id": self.product.pk, "quantity": 1}])
            delay.assert_not_called()
        self.assertEqual(response.status_code, 503)
        order = Order.objects.get(pk=response.data["order_id"])
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(order.stripe_session_id, f"cs_{order.pk}")
        self.assertEqual(order.payment_intent_id, f"pi_{order.pk}")
        self.assertEqual(order.checkout_url, "")
        self.assertEqual(StripeEvent.objects.filter(order=order).count(), 1)
        self.assertCountEqual(order.notifications.values_list("kind", flat=True), [Notification.Kind.ORDER_CREATED, Notification.Kind.PAID])
        self.assertEqual(delay.call_count, 2)

    @patch("shop.services.stripe.StripeClient")
    @patch("shop.tasks.send_order_notification.delay")
    def test_provider_failure_is_controlled_and_unpaid(self, delay, stripe_client):
        secret = "sentinel-stripe-credential"
        stripe_client.return_value.v1.checkout.sessions.create.side_effect = stripe.APIConnectionError(secret)
        with self.assertLogs("shop.services", level="ERROR") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.checkout([{"product_id": self.product.pk, "quantity": 1}])
        self.assertEqual(response.status_code, 503)
        order = Order.objects.get(pk=response.data["order_id"])
        self.assertEqual(order.status, Order.Status.CHECKOUT_FAILED)
        self.assertIsNone(order.stripe_session_id)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.notifications.get().kind, Notification.Kind.ORDER_CREATED)
        delay.assert_called_once()
        self.assertIn(str(order.pk), " ".join(logs.output))
        self.assertIn("APIConnectionError", " ".join(logs.output))
        self.assertNotIn(secret, " ".join(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))
