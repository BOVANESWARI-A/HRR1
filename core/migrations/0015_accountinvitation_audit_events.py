from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


def rename_audit_table_if_needed(apps, schema_editor):
    connection = schema_editor.connection
    tables = set(connection.introspection.table_names())
    old_table = 'core_audit trail'
    new_table = 'core_audittrail'
    if old_table in tables and new_table not in tables:
        schema_editor.alter_db_table(None, old_table, new_table)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_audittrail"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(
            rename_audit_table_if_needed,
            migrations.RunPython.noop,
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RenameModel(
                    old_name="Audit Trail",
                    new_name="AuditTrail",
                ),
            ],
            database_operations=[],
        ),
        migrations.CreateModel(
            name="AccountInvitation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254)),
                ("target_role", models.CharField(choices=[("candidate", "Candidate"), ("agent", "Agent"), ("hr", "HR")], max_length=20)),
                ("token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("active", models.BooleanField(default=True)),
                ("company", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="account_invitations", to="core.company")),
                ("invited_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="account_invitations", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-sent_at", "-id"]},
        ),
        migrations.AlterField(
            model_name="audittrail",
            name="action",
            field=models.CharField(
                choices=[
                    ("invite_sent", "Invitation Sent"),
                    ("submitted", "Application Submitted"),
                    ("access_requested", "Document Access Requested"),
                    ("access_approved", "Document Access Approved"),
                    ("view", "Application Viewed"),
                    ("download", "Application Downloaded"),
                    ("forwarded", "Candidate Profile Forwarded"),
                ],
                max_length=20,
            ),
        ),
    ]
