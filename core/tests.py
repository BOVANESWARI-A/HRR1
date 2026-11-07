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


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ForwardConfirmationTests(TestCase):
    def setUp(self):
        self.agent = User.objects.create_user(username="confirm-agent", email="agent@example.com")
        self.agent.groups.add(Group.objects.get_or_create(name="agent")[0])
        company = Company.objects.create(name="Confirmation Employer", email="hr-confirm@example.com", phone="123")
        candidate = Candidate.objects.create(full_name="Confirmation Candidate", email="candidate@example.com")
        AgentClient.objects.create(agent=self.agent, candidate=candidate)
        self.application = JobApplication.objects.create(candidate=candidate, company=company, status="submitted", submitted_at=timezone.now())
        Invitation.objects.create(application=self.application, invited_by=self.agent)
        self.client.force_login(self.agent)

    def test_share_popup_prefills_company_email_without_forwarding_on_page_load(self):
        response = self.client.get("/received-applications/")
        self.assertContains(response, 'data-company-email="hr-confirm@example.com"')
        self.assertContains(response, 'button.dataset.companyEmail || ""')
        self.assertContains(response, 'id="forwardApplicationModal"')
        self.assertContains(response, 'type="email" class="form-control" id="forwardRecipientEmail"')
        self.assertContains(response, 'Confirm &amp; Forward')
        self.assertNotContains(response, 'window.prompt')
        self.assertFalse(self.application.audit_trails.filter(action="forwarded").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_legacy_hr_company_uses_application_type_not_email(self):
        from .models import AgentHRClient
        hr = User.objects.create_user(username="legacy-hr", email="legacy-hr@example.com")
        hr.groups.add(Group.objects.get_or_create(name="hr")[0])
        UserProfile.objects.create(user=hr, company_name="", application_type="insitemy")
        Company.objects.get_or_create(name="InsiteMy", defaults={"email": hr.email, "phone": ""})
        AgentHRClient.objects.create(agent=self.agent, hr_user=hr, status="approved")
        response = self.client.get("/send-invite/")
        self.assertIn(("InsiteMy", "InsiteMy"), response.context["company_options"])
        self.client.post("/send-invitation/", {"recipient_role": "candidate", "company": hr.email, "emails": "new-candidate@example.com"})
        application = JobApplication.objects.get(candidate__email="new-candidate@example.com")
        self.assertEqual(application.company.name, "InsiteMy")
        self.assertFalse(Company.objects.filter(name=hr.email).exists())

    def test_popup_prefers_approved_hr_account_over_company_contact(self):
        from .models import AgentHRClient
        hr = User.objects.create_user(username="company-hr", email="actual-hr@example.com")
        hr.groups.add(Group.objects.get_or_create(name="hr")[0])
        UserProfile.objects.create(user=hr, company_name=self.application.company.name)
        AgentHRClient.objects.create(agent=self.agent, hr_user=hr, status="approved")
        response = self.client.get("/received-applications/")
        self.assertContains(response, 'data-company-email="actual-hr@example.com"')
        self.client.post(f"/application/{self.application.pk}/forward/", {"recipient_email": "actual-hr@example.com"})
        self.assertEqual(mail.outbox[-1].to, [hr.email])

    def test_old_forward_logs_do_not_move_application(self):
        AuditTrail.objects.create(application=self.application, user=self.agent, action="forwarded")
        self.assertIn(self.application, self.client.get("/received-applications/").context["applications"])
        self.assertNotIn(self.application, self.client.get("/submitted-applications/").context["applications"])

    def test_failed_email_does_not_confirm_or_move_application(self):
        from unittest.mock import patch
        with patch("core.views.send_mail", return_value=0):
            self.client.post(f"/application/{self.application.pk}/forward/", {"recipient_email": "confirmed@example.com"})
        self.assertFalse(self.application.audit_trails.filter(action="forward_confirmed").exists())
        self.assertIn(self.application, self.client.get("/received-applications/").context["applications"])

    def test_confirmed_email_is_used_and_application_moves(self):
        self.client.post(f"/application/{self.application.pk}/forward/", {"recipient_email": "confirmed@example.com"})
        self.assertEqual(mail.outbox[-1].to, ["confirmed@example.com"])
        self.assertNotIn(self.application, self.client.get("/received-applications/").context["applications"])
        self.assertIn(self.application, self.client.get("/submitted-applications/").context["applications"])


class HistoricalAutofillTests(TestCase):
    def setUp(self):
        self.candidate = Candidate.objects.create(full_name="Profile Name", email="profile@example.com", phone="5551234")
        self.insite, _ = Company.objects.get_or_create(name="InsiteMy", defaults={"email": "insite@example.com", "phone": ""})
        self.macro, _ = Company.objects.get_or_create(name="Macro Kiosk", defaults={"email": "macro@example.com", "phone": ""})
        self.current = JobApplication.objects.create(candidate=self.candidate, company=self.insite)

    def previous(self, data, company=None, status="submitted", candidate=None):
        return JobApplication.objects.create(candidate=candidate or self.candidate, company=company or self.insite, status=status, submitted_at=timezone.now(), data=data)

    def defaults(self):
        from .views import _candidate_defaults
        return _candidate_defaults(self.current)

    def test_combines_all_history_and_prefers_latest_nonempty_values(self):
        self.previous({"personal_information": {"name": "Older", "phone": "111", "nationality": "Malaysian"}, "reference": {"ref1name": "Referee"}})
        self.previous({"personal_information": {"name": "Newer", "phone": ""}, "employment_expectations": {"notice_period": "30 days"}})
        result = self.defaults()
        self.assertEqual(result["personal_information"]["name"], "Newer")
        self.assertEqual(result["personal_information"]["phone"], "111")
        self.assertEqual(result["personal_information"]["nationality"], "Malaysian")
        self.assertEqual(result["reference"]["ref1name"], "Referee")
        self.assertEqual(result["employment_expectations"]["notice_period"], "30 days")

    def test_maps_macro_fields_by_meaning_not_shared_field_name(self):
        self.previous({"macro_section_1": {"name": "Macro Name", "nationality_4": "01234567", "address": "Permanent", "address_2": "Current"}, "macro_section_2": {"height": "Employer Ltd"}, "macro_section_4": {"lastsalary_2": "9000", "expectedsalary": "Benefits", "ref1name_2": "Second Referee"}}, company=self.macro)
        result = self.defaults()
        self.assertEqual(result["personal_information"]["phone"], "01234567")
        self.assertEqual(result["personal_information"]["residential_address"], "Current")
        self.assertEqual(result["personal_information"]["permanent_address"], "Permanent")
        self.assertNotIn("height", result["personal_information"])
        self.assertEqual(result["employment_expectations"]["expectedsalary"], "9000")
        self.assertEqual(result["reference"]["ref2name"], "Second Referee")

    def test_current_draft_including_explicit_blanks_is_preserved(self):
        self.previous({"personal_information": {"name": "Old", "phone": "111"}, "reference": {"ref1name": "Old Ref"}})
        self.current.data = {"personal_information": {"name": "Edited", "phone": ""}, "reference": {"ref1name": ""}}
        result = self.defaults()
        self.assertEqual(result["personal_information"], {"name": "Edited", "phone": "", "email": self.candidate.email})
        self.assertEqual(result["reference"]["ref1name"], "")
        self.assertNotIn("email", self.current.data["personal_information"])

    def test_consent_confirmations_signatures_and_files_are_not_reused(self):
        self.previous({"personal_information": {"name": "Previous", "profile_photo": "old.jpg"}, "consent": {"consentname": "Old", "approved": True}, "declaration": {"applicant_signature": "old.png"}, "documents": {"updated_resume": "old.pdf"}, "iq_test": {"iq_q1": "old answer"}})
        result = self.defaults()
        for section in ("declaration", "documents"):
            self.assertNotIn(section, result)
        self.assertEqual(result["consent"], {"consentname": "Old"})
        self.assertEqual(result["iq_test"], {"iq_q1": "old answer"})
        self.assertNotIn("profile_photo", result["personal_information"])

    def test_review_sections_merge_history_without_reusing_consent_or_signatures(self):
        self.previous({"consent": {"consentname": "Older Name", "appname": "Older Applicant", "nric": "123456", "date": "2025-01-01", "approved": True}, "declaration": {"declaration_appname": "Older Applicant", "appplace": "Kuala Lumpur", "dateapplied": "2025-01-01", "applicant_signature": "old.png"}, "iq_test": {"iq_q1": "100", "iq_q2": "Older answer", "iq_response_2": "Older code", "iq_response_5": "SELECT * FROM older", "score": 99, "completed": True}})
        self.previous({"consent": {"consentname": "Newer Name", "nric": ""}, "iq_test": {"iq_q1": "144", "iq_response_2": "Newer code"}})
        result = self.defaults()
        self.assertEqual(result["consent"], {"consentname": "Newer Name", "appname": "Older Applicant", "nric": "123456"})
        self.assertEqual(result["declaration"], {"declaration_appname": "Older Applicant", "appplace": "Kuala Lumpur"})
        self.assertEqual(result["iq_test"], {"iq_q1": "144", "iq_q2": "Older answer", "iq_response_2": "Newer code", "iq_response_5": "SELECT * FROM older"})
        self.current.data = {"consent": {"consentname": ""}, "declaration": {"declaration_appname": "Edited"}, "iq_test": {"iq_q1": ""}}
        result = self.defaults()
        self.assertEqual(result["consent"]["consentname"], "")
        self.assertEqual(result["declaration"]["declaration_appname"], "Edited")
        self.assertEqual(result["iq_test"]["iq_q1"], "")

    def test_dynamic_declaration_and_pdpa_identity_reuse_without_confirmation(self):
        for slug in ("raddish", "nevigate"):
            company = Company.objects.create(name=slug.title(), email=f"{slug}@example.com")
            self.current.company = company
            self.previous({f"{slug}_application": {"declaration_name": "Prior Name", "declaration_nric": "123456", "pdpa_name": "Prior Name", "pdpa_nric": "123456", "declaration_confirmed": "true", "pdpa_confirmed": "true", "declaration_date": "2025-01-01", "pdpa_date": "2025-01-01", "applicant_signature": "old.png"}}, company=company)
            values = self.defaults()[f"{slug}_application"]
            self.assertEqual(values["declaration_name"], "Prior Name")
            self.assertEqual(values["declaration_nric"], "123456")
            if slug == "nevigate":
                self.assertEqual(values["pdpa_name"], "Prior Name")
            for key in ("declaration_confirmed", "pdpa_confirmed", "declaration_date", "pdpa_date", "applicant_signature"):
                self.assertNotIn(key, values)

    def test_other_candidates_and_unsubmitted_drafts_are_excluded(self):
        other = Candidate.objects.create(email="other@example.com")
        self.previous({"personal_information": {"phone": "private"}}, candidate=other)
        self.previous({"personal_information": {"phone": "draft"}}, status="draft")
        self.assertEqual(self.defaults()["personal_information"]["phone"], self.candidate.phone)

    def test_insite_to_macro_mapping_and_same_form_education(self):
        self.previous({"personal_information": {"phone": "0123", "height": "180"}, "employment_expectations": {"expectedsalary": "8000"}, "reference": {"ref2email": "ref@example.com"}})
        self.current.company = self.macro
        self.previous({"macro_section_2": {"height": "Employer"}, "macro_section_3": {"height": "University", "qualification": "Degree"}, "macro_section_5": {"macro_5_consent": "true"}}, company=self.macro)
        result = self.defaults()
        self.assertEqual(result["macro_section_1"]["nationality_4"], "0123")
        self.assertEqual(result["macro_section_2"]["height"], "Employer")
        self.assertEqual(result["macro_section_3"]["height"], "University")
        self.assertEqual(result["macro_section_4"]["lastsalary_2"], "8000")
        self.assertEqual(result["macro_section_4"]["ref1email_2"], "ref@example.com")
        self.assertNotIn("macro_section_5", result)

    def test_cross_form_recency_and_empty_lists_fill_from_older_forms(self):
        self.previous({"macro_section_1": {"name": "Older Macro", "nationality_4": "0123"}}, company=self.macro)
        self.previous({"personal_information": {"name": "Newest Insite", "phone": ["", None]}})
        result = self.defaults()["personal_information"]
        self.assertEqual(result["name"], "Newest Insite")
        self.assertEqual(result["phone"], "0123")

    def test_legacy_flat_identity_is_supported_without_copying_ambiguous_fields(self):
        self.previous({"name": "Flat Name", "nationality_4": "0123", "height": "Ambiguous"}, company=self.macro)
        result = self.defaults()["personal_information"]
        self.assertEqual(result["name"], "Flat Name")
        self.assertEqual(result["phone"], "0123")
        self.assertNotIn("height", result)

    def test_language_and_checkbox_answers_carry_within_same_form(self):
        company = Company.objects.create(name="Raddish", email="raddish@example.com")
        self.current.company = company
        self.previous({"raddish_application": {"name": "Prior", "field_fair_1": "true", "field_excellent_4": "true", "declaration_confirmed": "true", "declaration_name": "Prior"}}, company=company)
        values = self.defaults()["raddish_application"]
        self.assertEqual(values["field_fair_1"], "true")
        self.assertEqual(values["field_excellent_4"], "true")
        self.assertNotIn("declaration_confirmed", values)
        self.current = JobApplication.objects.create(candidate=self.candidate, company=self.macro)
        self.previous({"macro_section_1": {"macro_1_checkbox_10": "With Parents"}, "macro_section_4": {"macro_4_checkbox_3": "Yes", "macro_4_checkbox_4": ""}}, company=self.macro)
        result = self.defaults()
        self.assertEqual(result["macro_section_1"]["macro_1_checkbox_10"], "With Parents")
        self.assertEqual(result["macro_section_4"]["macro_4_checkbox_3"], "Yes")

    def test_reference_details_map_across_form_types(self):
        for slug in ("raddish", "nevigate"):
            company = Company.objects.create(name=slug.title(), email=f"{slug}@example.com")
            self.current.company = company
            self.previous({"reference": {"ref1name": "Ref One", "ref1pos": "Manager", "ref1com": "Acme", "ref1email": "one@example.com", "ref1phn": "111", "ref2name": "Ref Two", "ref2pos": "Director", "ref2com": "Beta", "ref2email": "two@example.com", "ref2phn": "222"}})
            values = self.defaults()[f"{slug}_application"]
            self.assertEqual(values["ref1name"], "Ref One")
            self.assertEqual(values["ref1com"], "Acme")
            self.assertEqual(values["ref1name__2"], "Ref Two")
            self.assertEqual(values["ref1email__2"], "two@example.com")
            self.assertEqual(values["ref1phn__3"], "222")
            self.assertNotIn("ref1phn__2", values)
            JobApplication.objects.all().delete()
            self.current = JobApplication.objects.create(candidate=self.candidate, company=self.insite)
            self.previous({f"{slug}_application": {"ref1name": "Dyn One", "ref1phn": "333", "ref1name__2": "Dyn Two", "ref1phn__3": "444"}}, company=company)
            reference = self.defaults()["reference"]
            self.assertEqual(reference["ref1name"], "Dyn One")
            self.assertEqual(reference["ref2name"], "Dyn Two")
            self.assertEqual(reference["ref2phn"], "444")

    def test_raddish_and_nevigate_forms_receive_history_for_prefilling(self):
        from .models import HROnboardingForm
        user = User.objects.create_user(username="history-candidate", email=self.candidate.email)
        self.candidate.user = user
        self.candidate.save()
        self.client.force_login(user)
        for form_type, company_name in (("raddish", "Raddish"), ("nevigate", "Nevigate")):
            company = Company.objects.create(name=company_name, email="hr@example.com", phone="")
            self.previous({f"{form_type}_application": {"name": "Historical Candidate", "qualification": "Degree", "declaration_confirmed": "true"}}, company=company)
            current = JobApplication.objects.create(candidate=self.candidate, company=company)
            invitation = Invitation.objects.create(application=current)
            if form_type == "nevigate":
                hr = User.objects.create_user(username="history-hr")
                UserProfile.objects.create(user=hr, company_name=company_name)
                HROnboardingForm.objects.create(hr_user=hr, company_name=company_name, employment_form="test.pdf", status="ready", connected_form_slug=form_type)
            response = self.client.get(f"/candidate/application/{invitation.token}/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["form_saved_data"]["qualification"], "Degree")
            self.assertNotIn("declaration_confirmed", response.context["form_saved_data"])
            current.refresh_from_db()
            self.assertEqual(current.data, {})

    def test_insite_page_receives_history_and_does_not_mutate_previous_application(self):
        previous = self.previous({"reference": {"ref1name": "Historical Ref"}, "personal_information": {"phone": "0123"}})
        original = previous.data.copy()
        user = User.objects.create_user(username="insite-history-candidate", email=self.candidate.email)
        self.candidate.user = user
        self.candidate.save()
        invitation = Invitation.objects.create(application=self.current)
        self.client.force_login(user)
        response = self.client.get(f"/candidate/application/{invitation.token}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["saved_data"]["reference"]["ref1name"], "Historical Ref")
        previous.refresh_from_db()
        self.assertEqual(previous.data, original)

    def test_macro_photos_and_flat_form_confirmation_controls_are_not_reused(self):
        self.current.company = self.macro
        self.previous({"macro_section_1": {"name": "Previous", "macro_doc_1_1": "old-photo.jpg"}}, company=self.macro)
        self.assertNotIn("macro_doc_1_1", self.defaults()["macro_section_1"])
        company = Company.objects.create(name="Raddish", email="hr@example.com", phone="")
        self.current.company = company
        self.previous({"raddish_application": {"name": "Previous", "name__2": "Old signature name", "field_flexCheckDefault_1": "true", "declaration_confirmed": "true", "date": "2026-01-01"}}, company=company)
        values = self.defaults()["raddish_application"]
        self.assertNotIn("name__2", values)
        self.assertNotIn("field_flexCheckDefault_1", values)
        self.assertNotIn("declaration_confirmed", values)
        self.assertNotIn("date", values)

    def test_passport_is_not_copied_into_identity_card_field(self):
        self.previous({"personal_information": {"passportnumber": "A1234567"}})
        self.current.company = self.macro
        self.assertNotIn("passportnumber", self.defaults()["macro_section_1"])
        self.previous({"personal_information": {"passportnumber": "900101-10-1234"}})
        self.assertEqual(self.defaults()["macro_section_1"]["passportnumber"], "900101-10-1234")

    def test_legacy_completed_record_without_submission_timestamp_is_used(self):
        previous = self.previous({"personal_information": {"nationality": "Malaysian"}}, status="reviewed")
        JobApplication.objects.filter(pk=previous.pk).update(submitted_at=None)
        self.assertEqual(self.defaults()["personal_information"]["nationality"], "Malaysian")

    def test_false_and_zero_are_retained(self):
        self.previous({"employment_expectations": {"own_transport": False, "lastsalary": 0}})
        result = self.defaults()["employment_expectations"]
        self.assertIs(result["own_transport"], False)
        self.assertEqual(result["lastsalary"], 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CandidateInvitationIdentityTests(TestCase):
    def setUp(self):
        self.email = "identity@example.com"
        self.legacy = Candidate.objects.create(email=self.email, full_name="Legacy")
        self.company, _ = Company.objects.get_or_create(name="InsiteMy")
        self.hr = User.objects.create_user(username="identity-hr", email="hr-identity@example.com")
        self.hr.groups.add(Group.objects.get_or_create(name="HR")[0])
        UserProfile.objects.create(user=self.hr, application_type="insitemy", company_name=self.company.name)

    def account(self):
        user = User.objects.create_user(username=self.email, email=self.email)
        user.groups.add(Group.objects.get_or_create(name="candidate")[0])
        return user

    def send(self):
        self.client.force_login(self.hr)
        return self.client.post("/send-invitation/", {"recipient_role": "candidate", "emails": self.email.upper()})

    def test_new_invitation_prefers_account_linked_candidate_over_old_duplicate(self):
        user = self.account()
        canonical = Candidate.objects.create(user=user, email=self.email)
        self.assertEqual(self.send().status_code, 302)
        invitation = Invitation.objects.latest("pk")
        self.assertEqual(invitation.application.candidate_id, canonical.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.client.force_login(user)
        response = self.client.get(f"/candidate/invitation/{invitation.token}/")
        self.assertRedirects(response, f"/candidate/application/{invitation.token}/", fetch_redirect_response=False)
        self.legacy.refresh_from_db()
        self.assertIsNone(self.legacy.user_id)

    def test_existing_account_without_profile_reuses_unlinked_candidate(self):
        user = self.account()
        self.send()
        self.legacy.refresh_from_db()
        self.assertEqual(self.legacy.user_id, user.pk)
        self.assertEqual(Candidate.objects.filter(email__iexact=self.email).count(), 1)
        self.assertEqual(Invitation.objects.latest("pk").application.candidate_id, self.legacy.pk)

    def test_standalone_signup_reuses_candidate_and_existing_invitation(self):
        app = JobApplication.objects.create(candidate=self.legacy, company=self.company, status="invited")
        invitation = Invitation.objects.create(application=app, invited_by=self.hr)
        response = self.client.post("/signup/", {"userRole": "jobSeeker", "address": "Registered Candidate", "email": self.email, "pass": "StrongPass123!", "cpass": "StrongPass123!", "accept": "true"})
        self.assertEqual(response.status_code, 302)
        self.legacy.refresh_from_db()
        self.assertIsNotNone(self.legacy.user_id)
        self.assertEqual(Candidate.objects.filter(email__iexact=self.email).count(), 1)
        self.assertRedirects(self.client.get(f"/candidate/invitation/{invitation.token}/"), f"/candidate/application/{invitation.token}/", fetch_redirect_response=False)

    def test_account_invite_signup_reuses_unlinked_candidate(self):
        from .models import AccountInvitation
        invite = AccountInvitation.objects.create(email=self.email, target_role="candidate", company=self.company, invited_by=self.hr)
        response = self.client.post(f"/signup/?account_invite={invite.token}", {"address": "Candidate Name", "email": self.email, "pass": "StrongPass123!", "cpass": "StrongPass123!", "accept": "true"})
        self.assertEqual(response.status_code, 302)
        self.legacy.refresh_from_db()
        self.assertIsNotNone(self.legacy.user_id)
        self.assertEqual(Candidate.objects.filter(email__iexact=self.email).count(), 1)

    def test_ambiguous_accounts_do_not_get_a_guessed_invitation(self):
        self.account()
        other = User.objects.create_user(username="duplicate-identity", email=self.email)
        other.groups.add(Group.objects.get_or_create(name="candidate")[0])
        self.send()
        self.assertFalse(Invitation.objects.exists())
        self.assertFalse(mail.outbox)

    def test_agent_invitation_links_the_canonical_candidate(self):
        from .models import AgentHRClient
        owner = self.account()
        canonical = Candidate.objects.create(user=owner, email=self.email)
        agent = User.objects.create_user(username="identity-agent")
        agent.groups.add(Group.objects.get_or_create(name="Agent")[0])
        AgentHRClient.objects.create(agent=agent, hr_user=self.hr, status="approved")
        self.client.force_login(agent)
        self.client.post("/send-invitation/", {"recipient_role": "candidate", "company": self.company.name, "emails": self.email})
        self.assertEqual(Invitation.objects.latest("pk").application.candidate_id, canonical.pk)
        self.assertTrue(AgentClient.objects.filter(agent=agent, candidate=canonical).exists())
        self.assertFalse(AgentClient.objects.filter(agent=agent, candidate=self.legacy).exists())

    def test_wrong_candidate_remains_denied(self):
        owner = self.account()
        candidate = Candidate.objects.create(user=owner, email=self.email)
        app = JobApplication.objects.create(candidate=candidate, company=self.company)
        invitation = Invitation.objects.create(application=app)
        other = User.objects.create_user(username="unrelated-candidate", email="unrelated@example.com")
        other.groups.add(Group.objects.get_or_create(name="candidate")[0])
        Candidate.objects.create(user=other, email=other.email)
        self.client.force_login(other)
        self.assertRedirects(self.client.get(f"/candidate/invitation/{invitation.token}/"), "/dashboard/", fetch_redirect_response=False)
        self.assertRedirects(self.client.get(f"/candidate/application/{invitation.token}/"), "/dashboard/", fetch_redirect_response=False)
        app.refresh_from_db()
        self.assertEqual(app.candidate_id, candidate.pk)


class ApplicationAgencyAuditTests(TestCase):
    def setUp(self):
        self.candidate_user = User.objects.create_user(username="direct-candidate", email="direct@example.com")
        self.candidate = Candidate.objects.create(user=self.candidate_user, email=self.candidate_user.email)
        self.company = Company.objects.create(name="InsiteMy")
        self.hr = User.objects.create_user(username="inviting-hr")
        self.hr.groups.add(Group.objects.get_or_create(name="HR")[0])
        self.agent = User.objects.create_user(username="linked-agent")
        self.agent.groups.add(Group.objects.get_or_create(name="Agent")[0])
        self.app = JobApplication.objects.create(candidate=self.candidate, company=self.company, status="submitted", submitted_at=timezone.now())
        Invitation.objects.create(application=self.app, invited_by=self.hr)
        AgentClient.objects.create(agent=self.agent, candidate=self.candidate)

    def steps(self, role="candidate", user=None):
        from .views import _audit_modal_steps
        return _audit_modal_steps(self.app, role, user or self.candidate_user)

    def test_direct_hr_application_has_no_agent_step_despite_candidate_agent_link(self):
        titles = [step["title"] for step in self.steps()]
        self.assertNotIn("Forwarding to HR by Agent", titles)
        self.assertIn("Application Submitted", titles)
        self.assertIn("Application viewed / downloaded by HR Access", titles)
        self.assertFalse(any("Agent" in title for title in titles))

    def test_forwarded_application_retains_agent_step_with_real_timestamp(self):
        event = AuditTrail.objects.create(application=self.app, user=self.agent, action="forwarded")
        step = next(step for step in self.steps() if step["title"] == "Forwarding to HR by Agent")
        self.assertEqual(step["date"], event.created_at)

    def test_another_application_forward_does_not_add_agent_details(self):
        other = JobApplication.objects.create(candidate=self.candidate, company=self.company)
        AuditTrail.objects.create(application=other, user=self.agent, action="forwarded")
        self.assertNotIn("Forwarding to HR by Agent", [step["title"] for step in self.steps()])

    def test_candidate_modal_hides_direct_step_and_shows_actual_forward(self):
        self.client.force_login(self.candidate_user)
        response = self.client.get("/submitted-applications/")
        self.assertNotContains(response, "Forwarding to HR by Agent")
        AuditTrail.objects.create(application=self.app, user=self.agent, action="forwarded")
        response = self.client.get("/submitted-applications/")
        self.assertContains(response, "Forwarding to HR by Agent")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DynamicApplicationTests(TestCase):
    def setUp(self):
        import tempfile
        from .models import HROnboardingForm
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.user = User.objects.create_user(username="dynamic-candidate", email="dynamic@example.com")
        self.candidate = Candidate.objects.create(user=self.user, email=self.user.email, full_name="Candidate")
        self.client.force_login(self.user)
        self.applications = []
        for slug in ("raddish", "nevigate"):
            company = Company.objects.create(name=f"Client {len(self.applications) + 1}", email="hr@example.com")
            hr = User.objects.create_user(username=f"dynamic-{slug}-hr", email=f"{slug}@example.com")
            hr.groups.add(Group.objects.get_or_create(name="HR")[0])
            UserProfile.objects.create(user=hr, company_name=company.name)
            HROnboardingForm.objects.create(hr_user=hr, company_name=company.name, status="ready", connected_form_slug=slug, employment_form="test.pdf")
            app = JobApplication.objects.create(candidate=self.candidate, company=company, status="invited")
            invitation = Invitation.objects.create(application=app)
            self.applications.append((slug, app, invitation, hr))

    def test_drafts_keep_every_value_and_all_files_without_submitting(self):
        for slug, app, invitation, hr in self.applications:
            response = self.client.post(f"/candidate/application/{invitation.token}/", {
                "action": "draft", "name": "Edited", "address": "Home", "address__2": "Current",
                "qualification": ["Degree", "Diploma"], "skills": ["Python", "Django"],
                "updated_resume": [SimpleUploadedFile("one.doc", b"document"), SimpleUploadedFile("two.docx", b"second document")],
            })
            self.assertEqual(response.status_code, 302)
            app.refresh_from_db()
            invitation.refresh_from_db()
            self.assertEqual(app.status, "invited")
            self.assertIsNone(invitation.completed_at)
            values = app.data[f"{slug}_application"]
            self.assertEqual(values["address__2"], "Current")
            self.assertEqual(values["qualification"], ["Degree", "Diploma"])
            self.assertEqual(values["skills"], ["Python", "Django"])
            self.assertEqual(app.files.count(), 2)
            self.assertFalse(app.audit_trails.filter(action="submitted").exists())
            page = self.client.get(f"/candidate/application/{invitation.token}/")
            self.assertEqual(page.context["form_saved_data"]["name"], "Edited")

    def test_invalid_final_submission_or_upload_does_not_change_application(self):
        for slug, app, invitation, hr in self.applications:
            for values in ({"final_submit": "true", "name": "No email"}, {"action": "draft", "resume": SimpleUploadedFile("bad.html", b"<script>bad</script>")}, {"action": "draft", "dob": "not-a-date"}, {"action": "draft", "resume": SimpleUploadedFile("large.pdf", b"x" * (1024 * 1024 + 1))}):
                response = self.client.post(f"/candidate/application/{invitation.token}/", values)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form_errors"])
                app.refresh_from_db()
                invitation.refresh_from_db()
                self.assertEqual(app.data, {})
                self.assertEqual(app.status, "invited")
                self.assertIsNone(invitation.completed_at)
                self.assertFalse(app.files.exists())

    def test_final_submission_validates_declarations_and_is_immutable(self):
        for slug, app, invitation, hr in self.applications:
            values = {"final_submit": "true", "name": "Candidate", "email": self.user.email, "position_applied": "Developer", "declaration_name": "Candidate", "declaration_nric": "123456", "declaration_date": "2026-01-01", "declaration_confirmed": "true"}
            if slug == "nevigate":
                values.update(pdpa_name="Candidate", pdpa_nric="123456", pdpa_date="2026-01-01", pdpa_confirmed="true", pdpa_signature=SimpleUploadedFile("pdpa.png", self.png()))
            values["applicant_signature"] = SimpleUploadedFile("signature.png", self.png())
            response = self.client.post(f"/candidate/application/{invitation.token}/", values)
            self.assertEqual(response.status_code, 302)
            app.refresh_from_db()
            invitation.refresh_from_db()
            self.assertEqual(app.status, "submitted")
            self.assertIsNotNone(invitation.completed_at)
            self.assertEqual(app.audit_trails.filter(action="submitted").count(), 1)
            self.client.post(f"/candidate/application/{invitation.token}/", {"action": "draft", "name": "Overwrite"})
            app.refresh_from_db()
            self.assertEqual(app.data[f"{slug}_application"]["name"], "Candidate")

    def test_submitted_list_offers_view_and_pdf_download_for_both_forms(self):
        for slug, app, invitation, hr in self.applications:
            app.status = "submitted"
            app.save()
        response = self.client.get("/submitted-applications/")
        self.assertEqual(response.status_code, 200)
        for slug, app, invitation, hr in self.applications:
            self.assertContains(response, f'/application/{app.pk}/"')
            self.assertContains(response, f'/application/{app.pk}/?download=pdf')

    def test_extra_rows_restore_and_invalid_metadata_is_rejected(self):
        for slug, app, invitation, hr in self.applications:
            url = f"/candidate/application/{invitation.token}/"
            response = self.client.post(url, {"action": "draft", "repeat0_row1_qualification": "Second degree", "_repeat_counts": '{"repeat0":1}'})
            self.assertEqual(response.status_code, 302)
            page = self.client.get(url)
            self.assertEqual(page.context["form_saved_data"]["repeat0_row1_qualification"], "Second degree")
            self.assertEqual(page.context["form_saved_data"]["_repeat_counts"], {"repeat0": 1})
            response = self.client.post(url, {"action": "draft", "_repeat_counts": '{"repeat0":100000}'})
            self.assertTrue(response.context["form_errors"])

    def test_declaration_and_pdpa_controls_belong_to_master_form_outside_layout_scope(self):
        from html.parser import HTMLParser

        class FormControls(HTMLParser):
            def __init__(self):
                super().__init__()
                self.divs = []
                self.controls = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "div":
                    self.divs.append(attrs.get("class", "").split())
                if tag in {"input", "select", "textarea"} and attrs.get("name"):
                    self.controls[attrs["name"]] = (attrs, any("content-section" in classes for classes in self.divs))

            def handle_endtag(self, tag):
                if tag == "div" and self.divs:
                    self.divs.pop()

        for slug, app, invitation, hr in self.applications:
            response = self.client.get(f"/candidate/application/{invitation.token}/")
            parser = FormControls()
            parser.feed(response.content.decode())
            fields = ["declaration_name", "declaration_nric", "declaration_date", "declaration_confirmed", "applicant_signature"]
            if slug == "nevigate":
                fields += ["pdpa_name", "pdpa_nric", "pdpa_date", "pdpa_confirmed", "pdpa_signature"]
            for name in fields:
                attrs, inside_layout_scope = parser.controls[name]
                self.assertEqual(attrs.get("form"), "applicationForm", f"{slug}: {name} has no form association; inside .content-section: {inside_layout_scope}")
            self.assertContains(response, "document.querySelectorAll('[form=\"applicationForm\"]')", html=False)

    def test_validation_error_preserves_typed_declaration_and_pdpa_values(self):
        for slug, app, invitation, hr in self.applications:
            values = {"final_submit": "true", "name": "Candidate", "email": self.user.email, "position_applied": "Developer", "declaration_name": "Typed Name", "declaration_nric": "A1234567", "declaration_date": "2026-01-01", "declaration_confirmed": "true"}
            if slug == "nevigate":
                values.update(pdpa_name="Typed PDPA Name", pdpa_nric="A1234567", pdpa_date="2026-01-02", pdpa_confirmed="true")
            response = self.client.post(f"/candidate/application/{invitation.token}/", values)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form_errors"])
            self.assertTrue(all("Signature is required" in error for error in response.context["form_errors"]))
            for name, value in values.items():
                if name != "final_submit":
                    self.assertEqual(response.context["form_saved_data"][name], value)
            app.refresh_from_db()
            self.assertEqual(app.status, "invited")

    def test_client_scripts_have_valid_javascript_and_real_declaration_controls(self):
        import shutil
        import subprocess
        node = shutil.which("node")
        for slug, app, invitation, hr in self.applications:
            response = self.client.get(f"/candidate/application/{invitation.token}/")
            html = response.content.decode()
            self.assertContains(response, 'name="declaration_name"')
            self.assertContains(response, 'name="applicant_signature"')
            self.assertNotContains(response, '8948225654')
            scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
            self.assertTrue(scripts)
            if node:
                for script in scripts:
                    result = subprocess.run([node, "--check"], input=script, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_uploaded_pdf_pages_are_appended_and_images_are_embedded(self):
        from io import BytesIO
        from pypdf import PdfReader
        from reportlab.pdfgen.canvas import Canvas
        from .models import ApplicationFile
        for slug, app, invitation, hr in self.applications:
            document = BytesIO()
            canvas = Canvas(document)
            canvas.drawString(50, 700, "Attachment page preserved")
            canvas.save()
            ApplicationFile.objects.create(application=app, file=SimpleUploadedFile("certificate.pdf", document.getvalue()))
            ApplicationFile.objects.create(application=app, file=SimpleUploadedFile("photo.png", self.png()))
            response = self.client.get(f"/application/{app.pk}/?download=pdf")
            reader = PdfReader(BytesIO(response.content))
            self.assertIn("Attachment page preserved", reader.pages[-1].extract_text())
            self.assertTrue(any("/XObject" in page["/Resources"] for page in reader.pages))

    @staticmethod
    def png():
        import base64
        return base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aC1sAAAAASUVORK5CYII=")

    def test_detail_pdf_and_zip_include_saved_values_and_original_attachments(self):
        from io import BytesIO
        import zipfile
        from pypdf import PdfReader
        from .models import ApplicationFile
        for slug, app, invitation, hr in self.applications:
            app.data = {f"{slug}_application": {"name": "Real Candidate", "qualification__2": "Distinct qualification", "_field_labels": {"qualification__2": "Course Studied"}}}
            app.status = "submitted"
            app.save()
            image = ApplicationFile.objects.create(application=app, field_name="photo", file=SimpleUploadedFile("photo.png", self.png()))
            document = ApplicationFile.objects.create(application=app, field_name="resume", file=SimpleUploadedFile("resume.docx", b"original resume"))
            page = self.client.get(f"/application/{app.pk}/")
            expected = "core/raddish_application.html" if slug == "raddish" else "core/Nevigate-application.html"
            self.assertTemplateUsed(page, expected)
            self.assertContains(page, "Distinct qualification")
            self.assertContains(page, "Course Studied")
            self.assertContains(page, f"?file={document.pk}")
            pdf = self.client.get(f"/application/{app.pk}/?download=pdf")
            self.assertEqual(pdf["Content-Type"], "application/pdf")
            text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages)
            self.assertIn("Distinct qualification", text)
            self.assertIn("COURSE STUDIED", text)
            self.assertIn(document.file.name.rsplit('/', 1)[-1], text)
            bundle = self.client.get(f"/application/{app.pk}/?download=zip")
            with zipfile.ZipFile(BytesIO(bundle.content)) as archive:
                self.assertIn(f"application_{app.pk}.pdf", archive.namelist())
                self.assertEqual(archive.read(f"documents/{document.pk}_{document.file.name.rsplit('/', 1)[-1]}"), b"original resume")
                self.assertEqual(archive.read(f"documents/{image.pk}_{image.file.name.rsplit('/', 1)[-1]}"), self.png())
            download = self.client.get(f"/application/{app.pk}/?file={document.pk}&download=1")
            self.assertEqual(b"".join(download.streaming_content), b"original resume")
            download.close()

    def test_file_and_form_access_remain_company_and_candidate_scoped(self):
        from .models import ApplicationFile
        slug, app, invitation, hr = self.applications[0]
        file = ApplicationFile.objects.create(application=app, file=SimpleUploadedFile("private.pdf", b"private"))
        other = User.objects.create_user(username="other-candidate", email="other@example.com")
        Candidate.objects.create(user=other, email=other.email)
        self.client.force_login(other)
        for suffix in ("", "?download=pdf", "?download=zip", f"?file={file.pk}"):
            self.assertEqual(self.client.get(f"/application/{app.pk}/{suffix}").status_code, 404)
        self.client.force_login(hr)
        self.assertEqual(self.client.get(f"/application/{app.pk}/?file={file.pk}").status_code, 302)
        ApplicationAccessRequest.objects.create(application=app, candidate=app.candidate, requested_by=hr, status="approved")
        response = self.client.get(f"/application/{app.pk}/?file={file.pk}")
        self.assertEqual(response.status_code, 200)
        response.close()
        foreign = self.applications[1][1]
        self.assertEqual(self.client.get(f"/application/{foreign.pk}/?download=zip").status_code, 404)


def _table_column_counts(html):
    table = re.search(r'id="data-table-1".*?</table>', html, re.S)
    self_html = table.group(0) if table else html
    header = re.search(r"<thead.*?</thead>", self_html, re.S)
    body_row = re.search(r"<tbody>.*?<tr.*?</tr>", self_html, re.S)
    header_cells = len(re.findall(r"<th\b", header.group(0))) if header else 0
    body_cells = len(re.findall(r"<td\b", body_row.group(0))) if body_row else 0
    return header_cells, body_cells
