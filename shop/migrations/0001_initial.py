import decimal
import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = [migrations.swappable_dependency(settings.AUTH_USER_MODEL)]

    operations = [
        migrations.CreateModel(
            name="Product",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True)),
                ("category", models.CharField(choices=[("book", "Book"), ("table", "Table"), ("laptop", "Laptop"), ("phone", "Phone"), ("accessory", "Accessory"), ("other", "Other")], max_length=16)),
                ("price", models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(decimal.Decimal("0.01"))])),
                ("is_active", models.BooleanField(default=True)),
            ],
            options={
                "ordering": ["id"],
                "constraints": [models.CheckConstraint(condition=models.Q(price__gt=0), name="product_price_positive")],
            },
        ),
        migrations.CreateModel(
            name="Order",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("pending", "Pending payment"), ("paid", "Paid"), ("failed", "Payment failed"), ("expired", "Checkout expired"), ("checkout_failed", "Checkout creation failed")], default="pending", max_length=20)),
                ("currency", models.CharField(default="usd", editable=False, max_length=3)),
                ("total", models.DecimalField(decimal_places=2, max_digits=12)),
                ("stripe_session_id", models.CharField(blank=True, max_length=255, null=True, unique=True)),
                ("payment_intent_id", models.CharField(blank=True, max_length=255, null=True, unique=True)),
                ("checkout_url", models.TextField(blank=True)),
                ("last_payment_event_created", models.PositiveBigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="orders", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
                "constraints": [models.CheckConstraint(condition=models.Q(total__gt=0), name="order_total_positive")],
            },
        ),
        migrations.CreateModel(
            name="OrderItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("product_name", models.CharField(max_length=200)),
                ("unit_price", models.DecimalField(decimal_places=2, max_digits=10)),
                ("quantity", models.PositiveSmallIntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(99)])),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="items", to="shop.order")),
                ("product", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to="shop.product")),
            ],
            options={"constraints": [
                models.UniqueConstraint(fields=("order", "product"), name="unique_order_product"),
                models.CheckConstraint(condition=models.Q(quantity__gte=1, quantity__lte=99), name="item_quantity_range"),
                models.CheckConstraint(condition=models.Q(unit_price__gt=0), name="item_price_positive"),
            ]},
        ),
        migrations.CreateModel(
            name="StripeEvent",
            fields=[
                ("id", models.CharField(max_length=255, primary_key=True, serialize=False)),
                ("event_type", models.CharField(max_length=100)),
                ("provider_created", models.PositiveBigIntegerField()),
                ("processed_at", models.DateTimeField(auto_now_add=True)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payment_events", to="shop.order")),
            ],
        ),
        migrations.CreateModel(
            name="Notification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("kind", models.CharField(choices=[("order_created", "Order created"), ("paid", "Payment received"), ("failed", "Payment failed"), ("expired", "Checkout expired")], max_length=20)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("sent", "Sent"), ("failed", "Delivery failed")], default="pending", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("order", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="notifications", to="shop.order")),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("order", "kind"), name="unique_order_notification")]},
        ),
    ]
