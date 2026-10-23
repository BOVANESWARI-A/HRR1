from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0020_candidate_archived'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='company_name',
            field=models.CharField(blank=True, default='', max_length=150),
        ),
        migrations.CreateModel(
            name='AgentHRClient',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('agent', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_hr_clients', to=settings.AUTH_USER_MODEL)),
                ('hr_user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='agent_links_as_hr', to=settings.AUTH_USER_MODEL)),
            ],
            options={'constraints': [models.UniqueConstraint(fields=('agent', 'hr_user'), name='unique_agent_hr_client')]},
        ),
        migrations.CreateModel(
            name='HROnboardingForm',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('company_name', models.CharField(max_length=150)),
                ('employment_form', models.FileField(upload_to='hr_employment_forms/%Y/%m/')),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('implementation_due_at', models.DateTimeField(blank=True, null=True)),
                ('hr_user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='onboarding_form', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
