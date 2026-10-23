import re

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (
    AgentClient,
    ApplicationAccessRequest,
    AuditTrail,
    Candidate,
    Company,
    Invitation,
    JobApplication,
    Notification,
    UserProfile,
)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="system@example.com",
)
class CompanyWorkflowTests(TestCase):
    def setUp(self):
        self.macro = Company.objects.create(
            name="Macro Kiosk",
            email="macro@example.com",
            phone="N/A",
        )
        self.insite = Company.objects.create(
            name="InsiteMy",
            email="insite@example.com",
            phone="N/A",
        )
        self.hr_group = Group.objects.create(name="hr")

    def create_hr(self, email, application_type):
        user = User.objects.create_user(
            username=email,
            email=email,
            password="StrongPass123!",
            first_name="HR",
        )
        user.groups.add(self.hr_group)
        UserProfile.objects.create(
            user=user,
            application_type=application_type,
        )
        return user

    def test_hr_login_is_company_aware(self):
        self.create_hr("macro.hr@example.com", "macro_kiosk")
        self.client.post(
            "/login/",
            {"email": "macro.hr@example.com", "password": "StrongPass123!"},
        )
        response = self.client.get("/login/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/applications/macro-kiosk/", response["Location"])

    def test_existing_email_cannot_register_again(self):
        self.create_hr("existing@example.com", "insitemy")
        response = self.client.post(
            "/signup/",
            {
                "userRole": "employer",
                "application_type": "macro_kiosk",
                "address": "Another Person",
                "email": "existing@example.com",
                "pass": "StrongPass123!",
                "cpass": "StrongPass123!",
                "accept": "true",
            },
        )
        self.assertEqual(User.objects.filter(email__iexact="existing@example.com").count(), 1)
        self.assertContains(response, "existing user", status_code=200)

    def test_hr_sends_company_specific_bulk_invites(self):
        self.create_hr("macro.hr@example.com", "macro_kiosk")
        self.client.login(username="macro.hr@example.com", password="StrongPass123!")
        response = self.client.post(
            "/send-invitation/",
            {
                "emails": "one@example.com\ntwo@example.com",
                "name": "Candidate",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(JobApplication.objects.filter(company=self.macro).count(), 2)
        self.assertEqual(Invitation.objects.filter(invited_by__email="macro.hr@example.com").count(), 2)
        self.assertEqual(len(mail.outbox), 2)

    def test_profile_photo_upload(self):
        user = self.create_hr("photo@example.com", "insitemy")
        self.client.login(username="photo@example.com", password="StrongPass123!")
        png = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01"
            b"\xff\x89\x99=\x1c\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        response = self.client.post(
            "/myprofile/",
            {"fname": "HR", "email": "photo@example.com", "profile_photo": SimpleUploadedFile("photo.png", png, content_type="image/png")},
        )
        self.assertEqual(response.status_code, 302)
        profile = UserProfile.objects.get(user=user)
        self.assertTrue(profile.profile_photo)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="system@example.com",
)
class AuditModalTests(TestCase):
    def setUp(self):
        self.company, _ = Company.objects.get_or_create(
            name="Macro Kiosk",
            defaults={"email": "macro@example.com", "phone": "N/A"},
        )
        self.hr_group = Group.objects.create(name="hr")
        self.agent_group = Group.objects.create(name="agent")
        self.candidate_group = Group.objects.create(name="candidate")
        self.admin_group = Group.objects.create(name="administration")

        self.hr = User.objects.create_user(
            username="hr.audit@example.com",
            email="hr.audit@example.com",
            password="StrongPass123!",
            first_name="Hema",
        )
        self.hr.groups.add(self.hr_group)
        UserProfile.objects.create(user=self.hr, application_type="macro_kiosk")

        self.agent = User.objects.create_user(
            username="agent.audit@example.com",
            email="agent.audit@example.com",
            password="StrongPass123!",
            first_name="Arun",
        )
        self.agent.groups.add(self.agent_group)

        self.candidate_user = User.objects.create_user(
            username="candidate.audit@example.com",
            email="candidate.audit@example.com",
            password="StrongPass123!",
            first_name="Cara",
        )
        self.candidate_user.groups.add(self.candidate_group)
        # The Candidate model still declares profile_data, while the migrations
        # removed that column. Add it only for this test database.
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE core_candidate "
                "ADD COLUMN IF NOT EXISTS profile_data jsonb NOT NULL DEFAULT '{}'::jsonb"
            )
        self.candidate = Candidate.objects.create(
            user=self.candidate_user,
            full_name="Cara Candidate",
            email="candidate.audit@example.com",
            phone="9000000000",
        )
        AgentClient.objects.create(agent=self.agent, candidate=self.candidate)

        self.admin = User.objects.create_user(
            username="admin.audit@example.com",
            email="admin.audit@example.com",
            password="StrongPass123!",
        )
        self.admin.groups.add(self.admin_group)

        opened = timezone.datetime(2026, 3, 1, 10, 15, tzinfo=timezone.get_current_timezone())
        submitted = timezone.datetime(2026, 3, 2, 11, 30, tzinfo=timezone.get_current_timezone())
        approved = timezone.datetime(2026, 3, 4, 9, 0, tzinfo=timezone.get_current_timezone())
        self.application = JobApplication.objects.create(
            candidate=self.candidate,
            company=self.company,
            status="submitted",
            submitted_at=submitted,
            data={},
        )
        Invitation.objects.create(
            application=self.application,
            invited_by=self.agent,
            sent_at=opened,
            opened_at=opened,
            active=False,
        )
        ApplicationAccessRequest.objects.create(
            application=self.application,
            candidate=self.candidate,
            requested_by=self.agent,
            status="approved",
            approved_at=approved,
        )
        ApplicationAccessRequest.objects.create(
            application=self.application,
            candidate=self.candidate,
            requested_by=self.hr,
            status="approved",
            approved_at=approved,
        )
        AuditTrail.objects.create(
            application=self.application,
            user=self.hr,
            action="hr_viewed",
            ip_address="203.0.113.10",
            location="Chennai, Tamil Nadu, India",
        )
        AuditTrail.objects.create(
            application=self.application,
            user=self.agent,
            action="forwarded",
            ip_address="198.51.100.8",
            location="Bengaluru, Karnataka, India",
        )
        Notification.objects.create(
            recipient=self.agent,
            application=self.application,
            title="Application forwarded",
            message=f"Application #{self.application.id} was forwarded to hr.desk@example.com.",
        )

    def test_hr_submitted_page_uses_candidate_audit_modal(self):
        self.client.login(username="hr.audit@example.com", password="StrongPass123!")
        response = self.client.get("/submitted-applications/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'id="auditModal{self.application.id}"')
        self.assertContains(response, "Invite Received")
        self.assertContains(response, "Application Submitted to HR")
        self.assertContains(response, "Access Request Received")
        self.assertContains(response, "Access Granted")
        self.assertContains(response, "HR IP Address")
        self.assertContains(response, "203.0.113.10")
        self.assertContains(response, "Chennai, Tamil Nadu, India")
        self.assertContains(response, "01/03/2026 10:15:00")
        self.assertNotContains(response, f"/application/{self.application.id}/audit/")

    def test_agent_received_page_has_no_audit_column(self):
        self.client.login(username="agent.audit@example.com", password="StrongPass123!")
        response = self.client.get("/received-applications/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'data-bs-target="#auditModal')
        self.assertNotContains(response, f"/application/{self.application.id}/audit/")

    def test_agent_submitted_page_uses_the_same_modal(self):
        self.client.login(username="agent.audit@example.com", password="StrongPass123!")
        response = self.client.get("/submitted-applications/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Application Submitted to Agent")
        self.assertContains(response, "Request Received")
        self.assertContains(response, "Access Granted")
        self.assertContains(response, "Agent IP Address")
        self.assertContains(response, "Forwarding to HR by Agent")
        self.assertContains(response, "hr.desk@example.com")
        self.assertContains(response, "Application viewed / downloaded by HR Access")
        self.assertContains(response, "203.0.113.10")
        self.assertContains(response, 'data-bs-target="#auditModal')

    def test_candidate_modal_keeps_existing_steps_and_adds_forward_steps(self):
        self.client.login(username="candidate.audit@example.com", password="StrongPass123!")
        response = self.client.get("/submitted-applications/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "HR Invite Received / Opened")
        self.assertContains(response, "Application Submitted")
        self.assertContains(response, "Candidate Approved")
        self.assertContains(response, "Forwarding to HR by Agent")
        self.assertContains(response, "Application viewed / downloaded by HR Access")
        self.assertContains(response, "203.0.113.10")
        header_cells, body_cells = _table_column_counts(response.content.decode())
        self.assertEqual(header_cells, body_cells)

    def test_admin_received_page_opens_the_modal(self):
        self.client.login(username="admin.audit@example.com", password="StrongPass123!")
        response = self.client.get("/received-applications/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Application Submitted to Agent")
        self.assertContains(response, "Forwarding to HR by Agent")
        self.assertNotContains(response, f"/application/{self.application.id}/audit/")

    def test_agent_view_is_saved_when_the_application_is_opened(self):
        self.client.login(username="agent.audit@example.com", password="StrongPass123!")
        response = self.client.get(f"/application/{self.application.id}/")
        self.assertEqual(response.status_code, 200)
        event = AuditTrail.objects.get(
            application=self.application,
            user=self.agent,
            action="agent_viewed",
        )
        self.assertEqual(event.ip_address, "127.0.0.1")
        self.assertEqual(event.location, "Local / private network")

    def test_candidate_login_opens_the_dashboard(self):
        draft = JobApplication.objects.create(
            candidate=self.candidate,
            company=self.company,
            status="invited",
            data={},
        )
        Invitation.objects.create(
            application=draft,
            invited_by=self.agent,
            active=True,
        )
        response = self.client.post(
            "/login/",
            {"email": "candidate.audit@example.com", "password": "StrongPass123!"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/dashboard/")


def _table_column_counts(html):
    table = re.search(r'id="data-table-1".*?</table>', html, re.S)
    self_html = table.group(0) if table else html
    header = re.search(r"<thead.*?</thead>", self_html, re.S)
    body_row = re.search(r"<tbody>.*?<tr.*?</tr>", self_html, re.S)
    header_cells = len(re.findall(r"<th\b", header.group(0))) if header else 0
    body_cells = len(re.findall(r"<td\b", body_row.group(0))) if body_row else 0
    return header_cells, body_cells
