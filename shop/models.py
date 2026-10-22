import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Product(models.Model):
    class Category(models.TextChoices):
        BOOK = "book", "Book"
        TABLE = "table", "Table"
        LAPTOP = "laptop", "Laptop"
        PHONE = "phone", "Phone"
        ACCESSORY = "accessory", "Accessory"
        OTHER = "other", "Other"

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=16, choices=Category.choices)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.CheckConstraint(condition=models.Q(price__gt=0), name="product_price_positive")]


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending payment"
        PAID = "paid", "Paid"
        FAILED = "failed", "Payment failed"
        EXPIRED = "expired", "Checkout expired"
        CHECKOUT_FAILED = "checkout_failed", "Checkout creation failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    currency = models.CharField(max_length=3, default="usd", editable=False)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    stripe_session_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    payment_intent_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    checkout_url = models.TextField(blank=True)
    last_payment_event_created = models.PositiveBigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(condition=models.Q(total__gt=0), name="order_total_positive")]


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    product_name = models.CharField(max_length=200)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(99)])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "product"], name="unique_order_product"),
            models.CheckConstraint(condition=models.Q(quantity__gte=1, quantity__lte=99), name="item_quantity_range"),
            models.CheckConstraint(condition=models.Q(unit_price__gt=0), name="item_price_positive"),
        ]


class StripeEvent(models.Model):
    id = models.CharField(primary_key=True, max_length=255)
    event_type = models.CharField(max_length=100)
    provider_created = models.PositiveBigIntegerField()
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payment_events")
    processed_at = models.DateTimeField(auto_now_add=True)


class Notification(models.Model):
    class Kind(models.TextChoices):
        ORDER_CREATED = "order_created", "Order created"
        PAID = "paid", "Payment received"
        FAILED = "failed", "Payment failed"
        EXPIRED = "expired", "Checkout expired"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Delivery failed"

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="notifications")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["order", "kind"], name="unique_order_notification")]
