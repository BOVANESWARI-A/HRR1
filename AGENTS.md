# Project verification

- Windows Python interpreter: `venv/Scripts/python.exe`.
- Django checks: `venv/Scripts/python.exe manage.py check`.
- Normal test command: `venv/Scripts/python.exe manage.py test core --noinput`.
- Migration `0012_applicationaccessrequest` contains PostgreSQL-specific SQL, so normal SQLite test database creation fails. For isolated application-level tests on SQLite, set `USE_SQLITE=1` in the process environment and run:

```text
venv/Scripts/python.exe -c "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','stalwart_project.settings'); import django; django.setup(); from django.conf import settings; settings.MIGRATION_MODULES={'core':None}; from django.core.management import call_command; call_command('test','core.tests.CandidateInvitationIdentityTests','core.tests.DynamicApplicationTests','core.tests.HistoricalAutofillTests','core.tests.ApplicationAgencyAuditTests','core.tests.ForwardConfirmationTests',interactive=False)"
```

- This isolated command creates tables from current models; it does not validate migration history or change the working database. Validate migrations separately on PostgreSQL before deployment.
- Audit modal markup is shared through `core/templates/core/includes/audit-modal.html`; preserve its UI when changing audit-step data.
