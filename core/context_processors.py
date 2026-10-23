from .views import get_role, user_application_type
from .models import Notification, Company


def app_context(request):
    role = get_role(request.user)
    notifications = Notification.objects.none()
    notification_count = 0
    if request.user.is_authenticated:
        unread = Notification.objects.filter(
            recipient=request.user, is_read=False
        )
        notification_count = unread.count()
        notifications = unread.order_by("-created_at")[:8]
    return {
        "app_role": role,
        "app_application_type": user_application_type(request.user) if role == "hr" else "",
        "notifications": notifications,
        "notification_count": notification_count,
        "application_companies": Company.objects.filter(is_active=True, archived=False).order_by("name") if role == "administration" else Company.objects.none(),
    }
