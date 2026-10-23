
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from core import views


urlpatterns = [
    # Django admin
    path("admin/", admin.site.urls),

    # Application URLs
    path("", include("core.urls")),
]

# Serve uploaded media files during development
if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT,
    )
