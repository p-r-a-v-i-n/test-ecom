from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import stripe
from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase

from shop.models import Notification, Order, Product

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=["testserver"])
class AuthenticationTests(APITestCase):
    def test_registration_hashing_and_password_validation(self):
        rejected = self.client.post("/api/auth/register/", {
            "username": "reader", "email": "reader@example.com", "password": "123",
        }, format="json")
        self.assertEqual(rejected.status_code, 400)
        password = "Sufficiently-Complex-Password-873!"
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
    def test_provider_failure_is_controlled_and_unpaid(self, delay, stripe_client):
        stripe_client.return_value.v1.checkout.sessions.create.side_effect = stripe.APIConnectionError("provider unavailable")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.checkout([{"product_id": self.product.pk, "quantity": 1}])
        self.assertEqual(response.status_code, 503)
        order = Order.objects.get(pk=response.data["order_id"])
        self.assertEqual(order.status, Order.Status.CHECKOUT_FAILED)
        self.assertIsNone(order.stripe_session_id)
        self.assertEqual(order.items.count(), 1)
        self.assertEqual(order.notifications.get().kind, Notification.Kind.ORDER_CREATED)
        delay.assert_called_once()
