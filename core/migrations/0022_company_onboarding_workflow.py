from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [("core", "0021_hr_client_onboarding"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name="agenthrclient", name="status", field=models.CharField(choices=[("pending", "Pending"), ("approved", "Approved"), ("denied", "Denied")], default="pending", max_length=20)),
        migrations.AddField(model_name="agenthrclient", name="updated_at", field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now), preserve_default=False),
        migrations.AddField(model_name="hronboardingform", name="status", field=models.CharField(choices=[("pending", "Pending admin review"), ("processing", "Form implementation in progress"), ("ready", "Ready for HR login"), ("rejected", "Needs changes")], default="pending", max_length=20)),
        migrations.AddField(model_name="hronboardingform", name="connected_form_slug", field=models.CharField(blank=True, default="", max_length=80)),
        migrations.AddField(model_name="hronboardingform", name="admin_notes", field=models.TextField(blank=True, default="")),
        migrations.AddField(model_name="hronboardingform", name="ready_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.CreateModel(name="HROnboardingDocument", fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("file", models.FileField(upload_to="hr_onboarding_documents/%Y/%m/")),
            ("original_name", models.CharField(blank=True, default="", max_length=255)),
            ("uploaded_at", models.DateTimeField(auto_now_add=True)),
            ("onboarding", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="documents", to="core.hronboardingform")),
        ]),
    ]
