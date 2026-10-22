# E-commerce backend

Django 5.2, Django REST Framework and Simple JWT provide a JSON API; PostgreSQL stores the catalog and orders. Stripe-hosted Checkout collects payment, verified webhooks confirm outcomes, and Celery sends email through Django's email backend. Python 3.11+ is required.

## Configuration and operation

Create a virtual environment and install the declared dependencies in your deployment environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

Use `.env.example` as a configuration reference. The application reads process environment variables; it does **not** automatically load `.env`. Configure the environment for both the web process and worker through your deployment platform or a dotenv-capable process manager. Never commit real secrets. Required settings are:

| Variable | Purpose |
| --- | --- |
| `DJANGO_SECRET_KEY` | Long random Django/JWT signing secret; rotation invalidates existing tokens |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated API hostnames, without schemes |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | PostgreSQL database and credentials |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_SSLMODE` | Database endpoint; defaults: localhost, 5432, require |
| `CELERY_BROKER_URL` | Redis broker URL; use authenticated TLS (`rediss://`) outside trusted networks |
| `STRIPE_SECRET_KEY` | Stripe secret API key, matching the account/mode of the webhook endpoint |
| `STRIPE_WEBHOOK_SECRET` | Signing secret of this specific webhook endpoint |
| `STRIPE_SUCCESS_URL`, `STRIPE_CANCEL_URL` | Absolute merchant return URLs; success can include `{CHECKOUT_SESSION_ID}` |
| `DEFAULT_FROM_EMAIL` | Verified sender address |
| `EMAIL_BACKEND` | Django backend; defaults to SMTP |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | SMTP service endpoint and credentials |
| `EMAIL_USE_TLS`, `EMAIL_USE_SSL` | Enable at most one; defaults to TLS on port 587 |
| `LOG_LEVEL` | Console application/worker logging level; defaults to INFO |

Provision PostgreSQL and Redis, supply the environment, then apply committed migrations and run the processes:

```sh
python manage.py migrate
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --error-logfile -
celery -A config worker --loglevel=INFO
```

Run the web server and worker as separate managed processes using the same configuration. Terminate HTTPS at a correctly configured deployment endpoint and route requests to the web process. Secure redirects default to enabled; a proxy must forward the HTTPS scheme correctly (do not accept untrusted forwarding headers). `DJANGO_DEBUG` defaults to false. `DJANGO_SECURE_HSTS_SECONDS` defaults to zero; enable HSTS after verifying HTTPS. For an isolated non-HTTPS environment only, disable `DJANGO_SECURE_SSL_REDIRECT` and use the appropriate database SSL mode. Keep PostgreSQL and Redis private. SMTP timeout is 15 seconds. For a non-production environment, Django's console email backend can replace SMTP, but it prints email content.

The catalog is managed through Django's ORM or trusted data imports; there is no public catalog-write API. For example, in `python manage.py shell`:

```python
from shop.models import Product
Product.objects.create(name="Django book", category="book", price="19.99")
```

Supported categories are `book`, `table`, `laptop`, `phone`, `accessory` and `other`. This initial flow uses **USD only**, with two decimal places, quantities 1–99 and up to 100 distinct items per order. Checkout totals must be USD 0.50–999999.99. Disable a product with `is_active=False`; historical order items retain the purchased name, price and quantity. API clients cannot update orders or snapshots. Products referenced by purchases and users owning orders are protected from deletion.

## API

All routes have trailing slashes. Send JSON and use `Authorization: Bearer <access>` for protected routes. Catalog and order lists are paginated (`count`, `next`, `previous`, `results`; 50 entries per page).

| Method and route | Behavior |
| --- | --- |
| `POST /api/auth/register/` | `{ "username": "reader", "email": "reader@example.com", "password": "..." }`; Django password validation and hashing; 201 with id, username and email |
| `POST /api/auth/token/` | Username/password; 200 with `access` and `refresh`; invalid credentials: 401 |
| `POST /api/auth/token/refresh/` | `{ "refresh": "..." }`; 200 with new access token; invalid refresh token: 401 |
| `GET /api/products/` | Public active catalog; id, name, description, category, price (USD decimal string) |
| `GET /api/products/<id>/` | Public active product, or 404 |
| `POST /api/checkout/` | Authenticated; `{ "items": [{ "product_id": 1, "quantity": 2 }] }`; 201 with `order_id` and `checkout_url` |
| `GET /api/orders/` | Authenticated owner's orders with status, currency, total, items and creation time |
| `GET /api/orders/<uuid>/` | Owner only; another user's order returns 404 |
| `POST /api/stripe/webhook/` | Raw Stripe body plus `Stripe-Signature`; no JWT required |

Access tokens last 15 minutes and refresh tokens one day. Missing/invalid authentication on protected routes returns 401. Validation failures return 400. Unknown or inactive products, duplicate selections, empty carts and out-of-range quantities are rejected. Supplied prices/totals are ignored; stored prices determine both the snapshot and Stripe line items. Apply internet-facing rate limits at the deployment edge for registration, token and checkout endpoints.

Each checkout POST creates a new pending order. A Stripe creation error returns a controlled 503 with `order_id`; the order remains unpaid (`checkout_failed` unless a verified outcome has already arrived). Inspect that order before starting a new purchase: a provider timeout can mean Stripe created a session, and its subsequent webhook can recover the association. SDK network retries share an order-specific idempotency key. Separate client POSTs are separate purchases; no client retry/idempotency contract is provided.

## Stripe configuration and payment truth

Provision a Stripe account, API key and HTTPS **snapshot-event** webhook endpoint at `/api/stripe/webhook/`. Configure these four events in Stripe Workbench:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`
- `checkout.session.async_payment_failed`
- `checkout.session.expired`

Configure the endpoint's signing secret in both processes. Use matching test-mode credentials in staging and live-mode credentials in production. Pin the endpoint's API version to the version supported by the installed Stripe SDK and retain snapshot payloads with `data.object` Checkout Session fields. The return URLs belong to your storefront; that UI is outside this repository.

A redirect or created session never marks an order paid. `completed` with `payment_status=paid` and asynchronous success confirm payment; unpaid completion waits for the asynchronous outcome. Asynchronous failure marks `failed`; expiration marks `expired`. A paid order never regresses, including when a failure arrives later. Older non-success events cannot replace a newer outcome. Confirmed success can supersede an earlier failure even when delivered late.

The handler verifies the signature against raw bytes, associates the session/reference/metadata with its order, and checks currency and total before changing state. Order locks, unique event IDs and unique order/notification-kind pairs prevent repeated transitions and queueing on duplicates. Unsupported or unrelated verified events receive 200; invalid signatures/payloads receive 400. Database/processing errors receive 5xx so Stripe can retry. Session, PaymentIntent and event identifiers are stored; full provider payloads are not stored or logged. Follow [Stripe's webhook guidance](https://docs.stripe.com/webhooks) when configuring delivery, endpoint versions and operational retries.

## Notifications and logging

Order creation and each supported payment transition create a durable notification record in the same transaction, with Celery dispatch registered through Django `transaction.on_commit`. Emails go to the order owner's configured email address. Celery tasks send via Django, record successful delivery and skip already-sent records. Transient SMTP/network failures receive at most three retries after the first attempt, with bounded backoff. Exhausted notifications become `failed` and produce an actionable error log.

A failed broker publish leaves a `pending` notification. After restoring the broker, recover those records with:

```sh
python manage.py dispatch_notifications
```

It is safe to repeat this command; workers lock notification records and skip ones already sent. Inspect failed deliveries and SMTP configuration before deliberately resetting their status for another delivery attempt through trusted operations. Email transport is at least once: a worker crash after SMTP acceptance but before recording success can result in a duplicate email. There is no automatic broker-outage recovery scheduler in this slice; run the recovery command operationally.

Application logs go to stdout with timestamp, severity and logger. Request failures include method, resolved view and status; checkout/payment logs include order, session, payment and event identifiers as appropriate. Notification failures include the record identifier and recovery action. Provider exception text, request bodies, query strings, passwords, JWTs, credentials and card details are excluded from application logs. Raw Django/provider request loggers are suppressed in favor of these selected fields. Configure any proxy/access logging with the same exclusions; restrict access and retention for identifier-bearing logs.

Celery initialization follows its [Django integration guide](https://docs.celeryq.dev/en/stable/django/first-steps-with-django.html), and dispatch uses Django's [after-commit callbacks](https://docs.djangoproject.com/en/5.2/topics/db/transactions/).

## Verification scope

Focused Django tests in `shop/tests/` cover authentication, ownership, catalog selections, snapshot calculations, provider failures, signed webhook outcomes/deduplication/order, commit callbacks and email retries. Stripe, broker dispatch and SMTP are mocked or replaced with an in-memory backend. Signature verification uses locally constructed signed fixture bytes and the real verifier; it does not contact Stripe. The suite requires a provisioned PostgreSQL test database and permission to create it.

The ticket explicitly says **don't test locally**. No tests, local smoke checks or live integrations were run during implementation. For authorized CI, provide placeholder external credentials and PostgreSQL configuration using the environment above, then use Django's runner (`python manage.py test shop.tests`). Tests override HTTPS redirects and allowed hosts; they never need live Stripe, Redis or SMTP. No local verification command is required by this repository.

This PR covers catalog-to-payment and notifications. Storefront UI, fulfillment/shipping, taxes, refunds, subscriptions and inventory reservations are outside scope. External provisioning, deployment and live email/payment verification remain operational setup.
