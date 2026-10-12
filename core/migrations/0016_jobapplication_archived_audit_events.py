from django.db import migrations, models


def ensure_archive_columns(apps, schema_editor):
    connection = schema_editor.connection
    table_name = "core_jobapplication"

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor,
                table_name,
            )
        }

        if "archived" not in columns:
            schema_editor.execute(
                f'ALTER TABLE "{table_name}" '
                'ADD COLUMN "archived" boolean DEFAULT false NOT NULL'
            )

        if "archived_at" not in columns:
            schema_editor.execute(
                f'ALTER TABLE "{table_name}" '
                'ADD COLUMN "archived_at" timestamp with time zone NULL'
            )


def reverse_archive_columns(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0015_accountinvitation_audit_events"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    ensure_archive_columns,
                    reverse_archive_columns,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="jobapplication",
                    name="archived",
                    field=models.BooleanField(default=False),
                ),
                migrations.AddField(
                    model_name="jobapplication",
                    name="archived_at",
                    field=models.DateTimeField(
                        null=True,
                        blank=True,
                    ),
                ),
            ],
        ),

        migrations.AlterField(
            model_name="audittrail",
            name="action",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("invite_sent", "Invitation Sent"),
                    ("invite_viewed", "Invitation Viewed"),
                    ("application_filled", "Application Filled"),
                    ("submitted", "Application Submitted"),
                    ("access_requested", "Document Access Requested"),
                    ("access_approved", "Document Access Approved"),
                    ("view", "Application Viewed"),
                    ("download", "Application Downloaded"),
                    ("forwarded", "Candidate Profile Forwarded"),
                    ("archived", "Application Archived"),
                ],
            ),
        ),
    ]