import logging

import stripe
from django.conf import settings
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Order, Product
from .serializers import CheckoutSerializer, OrderSerializer, ProductSerializer, RegistrationSerializer
from .services import CheckoutUnavailable, apply_stripe_event, create_checkout

logger = logging.getLogger(__name__)


class RegisterView(generics.CreateAPIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    serializer_class = RegistrationSerializer


class ProductListView(generics.ListAPIView):
    permission_classes = [permissions.AllowAny]
    queryset = Product.objects.filter(is_active=True)
    serializer_class = ProductSerializer


class ProductDetailView(generics.RetrieveAPIView):
    permission_classes = [permissions.AllowAny]
    queryset = Product.objects.filter(is_active=True)
    serializer_class = ProductSerializer


class OwnerOrderMixin:
    serializer_class = OrderSerializer

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).prefetch_related("items")


class OrderListView(OwnerOrderMixin, generics.ListAPIView):
    pass


class OrderDetailView(OwnerOrderMixin, generics.RetrieveAPIView):
    pass


class CheckoutView(APIView):
    def post(self, request):
        serializer = CheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = create_checkout(request.user, serializer.validated_data["items"])
        except CheckoutUnavailable as exc:
            return Response(
                {"detail": "Checkout could not be created. Check your order before retrying.", "order_id": str(exc.order_id)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {"order_id": str(order.pk), "checkout_url": order.checkout_url},
            status=status.HTTP_201_CREATED,
        )


class StripeWebhookView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        try:
            event = stripe.Webhook.construct_event(
                request.body,
                request.headers.get("Stripe-Signature", ""),
                settings.STRIPE_WEBHOOK_SECRET,
            )
        except (ValueError, stripe.SignatureVerificationError):
            logger.warning("stripe_webhook_invalid_signature")
            return Response({"detail": "Invalid webhook signature or payload."}, status=400)
        try:
            apply_stripe_event(event)
        except (KeyError, TypeError, ValueError):
            logger.warning("stripe_webhook_invalid_event")
            return Response({"detail": "Invalid event."}, status=400)
        return Response({"received": True})
