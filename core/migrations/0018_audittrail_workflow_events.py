# Generated for the additional workflow audit labels.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0017_company_archived_company_archived_at")]

    operations = [
        migrations.AlterField(
            model_name="audittrail",
            name="action",
            field=models.CharField(max_length=20, choices=[
                ("invite_sent", "HR Invite Sent"), ("invite_viewed", "Invitation Viewed"),
                ("invite_received", "Invite Received"), ("application_filled", "Application Filled"),
                ("submitted", "Application Submitted"), ("access_requested", "Access Request Received"),
                ("access_approved", "Access Granted"), ("view", "Application Viewed"),
                ("download", "Application Downloaded"), ("forwarded", "Forwarded to HR by Agent"),
                ("hr_viewed", "Application Viewed by HR"), ("hr_downloaded", "Application Downloaded by HR"),
                ("archived", "Application Archived"),
            ]),
        ),
    ]
