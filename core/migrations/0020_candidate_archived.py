from django.db import migrations, models


def ensure_candidate_archived(apps, schema_editor):
    connection = schema_editor.connection
    table_name = "core_candidate"

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
        return

    schema_editor.execute(
        f'UPDATE "{table_name}" SET "archived" = false WHERE "archived" IS NULL'
    )
    schema_editor.execute(
        f'ALTER TABLE "{table_name}" ALTER COLUMN "archived" SET DEFAULT false'
    )
    schema_editor.execute(
        f'ALTER TABLE "{table_name}" ALTER COLUMN "archived" SET NOT NULL'
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0019_audittrail_agent_events")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(ensure_candidate_archived, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="candidate",
                    name="archived",
                    field=models.BooleanField(default=False),
                ),
            ],
        ),
    ]
