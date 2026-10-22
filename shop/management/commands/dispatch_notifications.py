from django.core.management.base import BaseCommand

from shop.models import Notification
from shop.notifications import enqueue_notification


class Command(BaseCommand):
    help = "Requeue pending notifications after a broker outage (safe to repeat)."

    def handle(self, *args, **options):
        for notification_id in Notification.objects.filter(status=Notification.Status.PENDING).values_list("pk", flat=True).iterator():
            enqueue_notification(notification_id)
