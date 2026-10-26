from functools import wraps
from pathlib import Path
import re
import ast
import json
from copy import deepcopy
import ipaddress
from urllib.request import Request as UrlRequest, urlopen
from django.http import JsonResponse



from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login as auth_login, logout as auth_logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core import signing
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.http import Http404, JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme


from .models import (
    AgentClient,
    AgentHRClient,
    HROnboardingForm,
    HROnboardingDocument,
    AppSetting,
    CandidateDocument,
    Company,
    ApplicationFile,
    Candidate,
    EmailLog,
    Feedback,
    Invitation,
    AccountInvitation,
    JobApplication,
    Notification,
    Subscription,
    UserProfile,
    EmploymentHistory,
    ApplicationAccessRequest,
    AuditTrail,
)

User = get_user_model()

ROLE_GROUPS = {
    "candidate": {"candidate", "Candidate"},
    "agent": {"agent", "Agent"},
    "hr": {"hr", "HR"},
    "administration": {"administration", "Administration", "admin", "Admin"},
}

APPLICATION_SECTIONS = {
    "personal_information",
    "iq_test",
    "consent",
    "documents",
    "employment_expectations",
    "reference",
    "declaration",
    "employment_history",
    "education",
    "general_information",
    "background_check",
    "general_assignment",
    "pdpa_consent",
}

IGNORED_POST_FIELDS = {"csrfmiddlewaretoken", "section", "save_section", "next", "final_submit"}

ALLOWED_UPLOAD_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".webp"
}
MAX_UPLOAD_SIZE = 1 * 1024 * 1024


def normalize_person_name(value):
    """Return one clean candidate name, including for legacy list strings."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        values = []
        for item in value:
            item = normalize_person_name(item)
            if item and item not in values:
                values.append(item)
        return " ".join(values).strip()
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, (list, tuple, set)):
                return normalize_person_name(parsed)
        except (ValueError, SyntaxError):
            pass
    return " ".join(text.split())


def get_role(user):
    if not user or not user.is_authenticated:
        return None
    if user.is_superuser:
        return "administration"
    groups = set(user.groups.values_list("name", flat=True))
    for role in ("administration", "hr", "agent", "candidate"):
        if groups.intersection(ROLE_GROUPS[role]):
            return role
    if user.is_staff:
        return "hr"
    return "candidate"


def _users_for_role(role):
    names = ROLE_GROUPS.get(role, set())
    users = list(User.objects.filter(groups__name__in=names).distinct())
    if role == "hr":
        # get_role() also treats staff users without a group as HR.
        users.extend(
            User.objects.filter(is_staff=True)
            .exclude(is_superuser=True)
            .exclude(groups__name__in=ROLE_GROUPS["administration"])
        )
    if role == "administration":
        users.extend(User.objects.filter(is_superuser=True))
    unique = {}
    for user in users:
        unique[user.pk] = user
    return list(unique.values())


def _notify_users(users, title, message, application=None, target_url=""):
    seen = set()
    for user in users:
        if not user or not user.is_authenticated or user.pk in seen:
            continue
        seen.add(user.pk)
        resolved_url = target_url or ""
        if application and resolved_url and resolved_url.startswith("/"):
            separator = "&" if "?" in resolved_url else "?"
            resolved_url = f"{resolved_url}{separator}highlight={application.pk}#application-{application.pk}"
        Notification.objects.create(
            recipient=user,
            title=title,
            message=message,
            application=application,
            target_url=resolved_url,
        )


def _notify_role(role, title, message, application=None, target_url=""):
    _notify_users(_users_for_role(role), title, message, application, target_url)


def _hr_users_for_company(company):
    """Return HR users assigned to this company only.

    A candidate can have applications for multiple companies, while each HR
    account is restricted to the company assigned in UserProfile.
    """
    if not company:
        return []
    users = []
    company_key = company.name.strip().casefold()
    for user in _users_for_role("hr"):
        profile = UserProfile.objects.filter(user=user).first()
        profile_company = (profile.company_name if profile else "").strip().casefold()
        if profile_company and profile_company == company_key:
            users.append(user)
        elif not profile_company:
            expected_type = "insitemy" if company_key == "insitemy" else "macro_kiosk"
            profile_type = profile.application_type if profile else "macro_kiosk"
            if profile_type == expected_type and company_key in {"insitemy", "macro kiosk"}:
                users.append(user)
    return users


def _notify_hr_company(company, title, message, application=None, target_url=""):
    _notify_users(_hr_users_for_company(company), title, message, application, target_url)


def _notify_candidate(candidate, title, message, application=None, target_url=""):
    user = getattr(candidate, "user", None) if candidate else None
    if user:
        _notify_users([user], title, message, application, target_url)


def _notify_candidate_agents(candidate, title, message, application=None, target_url=""):
    if not candidate:
        return
    users = User.objects.filter(agent_clients__candidate=candidate).distinct()
    _notify_users(users, title, message, application, target_url)


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return (request.META.get("REMOTE_ADDR") or "").strip()


def _ip_location(ip_address):
    if not ip_address:
        return "Unknown"
    try:
        ip = ipaddress.ip_address(ip_address)
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            return "Local / private network"
    except ValueError:
        return "Unknown"

    try:
        req = UrlRequest(
            f"https://ipapi.co/{ip_address}/json/",
            headers={"User-Agent": "Stalwart-Django-Audit/1.0"},
        )
        with urlopen(req, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        parts = [payload.get("city"), payload.get("region"), payload.get("country_name")]
        location = ", ".join(str(part).strip() for part in parts if part)
        return location or "Unknown"
    except Exception:
        return "Unknown"


def _record_hr_audit(request, app, action):
    if get_role(request.user) != "hr":
        return None
    ip_address = _client_ip(request)
    location = _ip_location(ip_address)
    return AuditTrail.objects.create(
        application=app,
        user=request.user,
        action=action,
        ip_address=ip_address or None,
        location=location,
    )


def _record_agent_audit(request, app, action):
    """Save an agent view or download. Other audit actions stay unchanged."""
    if get_role(request.user) != "agent":
        return None
    ip_address = _client_ip(request)
    location = _ip_location(ip_address)
    return AuditTrail.objects.create(
        application=app,
        user=request.user,
        action=action,
        ip_address=ip_address or None,
        location=location,
    )

def get_insitemy_company():
    return get_object_or_404(
        Company,
        name__iexact="InsiteMy",
        is_active=True,
    )


def get_macro_kiosk_company():
    return get_object_or_404(
        Company,
        name__iexact="Macro Kiosk",
        is_active=True,
    )


def user_application_type(user):
    if not user or not user.is_authenticated:
        return None
    if get_role(user) == "hr":
        profile, _ = UserProfile.objects.get_or_create(user=user)
        return profile.application_type or "macro_kiosk"
    return None


def hr_company_name(user):
    profile = UserProfile.objects.filter(user=user).first()
    if profile and profile.company_name.strip():
        return profile.company_name.strip()
    return "InsiteMy" if user_application_type(user) == "insitemy" else "Macro Kiosk"


def role_landing_url(user):
    return reverse("dashboard")


def is_insitemy_application(app):
    if not app.company:
        return False
    if app.company.name.strip().lower() == "insitemy":
        return True
    return HROnboardingForm.objects.filter(hr_user__profile__company_name__iexact=app.company.name, status="ready", connected_form_slug="insitemy").exists()


def is_macro_kiosk_application(app):
    if not app.company:
        return False
    if app.company.name.strip().lower() in {"macro kiosk", "macrokiosk"}:
        return True
    return HROnboardingForm.objects.filter(hr_user__profile__company_name__iexact=app.company.name, status="ready", connected_form_slug="macro_kiosk").exists()


def role_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        @login_required(login_url="/login/")
        def wrapped(request, *args, **kwargs):
            if get_role(request.user) not in allowed_roles:
                messages.error(request, "You are not authorized to access this page.")
                return redirect("dashboard")
            return view(request, *args, **kwargs)
        return wrapped
    return decorator


def safe_next_url(request, value):
    value = (value or "").strip()
    if not value:
        return ""
    allowed_hosts = {request.get_host()}
    return value if url_has_allowed_host_and_scheme(value, allowed_hosts=allowed_hosts, require_https=request.is_secure()) else ""


def candidate_for_user(user):
    if not user or not user.is_authenticated:
        return None
    candidate = getattr(user, "candidate_profile", None)
    if candidate:
        return candidate
    if user.email:
        return Candidate.objects.filter(email__iexact=user.email).first()
    return None


def candidate_queryset_for_user(user):
    role = get_role(user)
    if role in {"administration", "hr"}:
        return Candidate.objects.all()
    if role == "agent":
        ids = AgentClient.objects.filter(agent=user).values_list("candidate_id", flat=True)
        return Candidate.objects.filter(pk__in=ids)
    candidate = candidate_for_user(user)
    return Candidate.objects.filter(pk=candidate.pk) if candidate else Candidate.objects.none()


def dashboard_metrics(user):
    role = get_role(user)
    if role == "candidate":
        candidate = candidate_for_user(user)
        if not candidate:
            return {"role": "candidate", "account_type": "Free", "valid_till": None, "remaining_days": 0, "application_submitted": 0}
        subscription = Subscription.objects.filter(candidate=candidate).first()
        return {
            "role": "candidate",
            "account_type": subscription.account_type if subscription else "Free",
            "valid_till": subscription.valid_till.isoformat() if subscription and subscription.valid_till else None,
            "remaining_days": subscription.remaining_days if subscription else 0,
            "application_submitted": JobApplication.objects.filter(candidate=candidate, status="submitted").count(),
        }
    if role == "agent":
        ids = AgentClient.objects.filter(agent=user).values_list("candidate_id", flat=True)
        return {
            "role": "agent",
            "client_count": Candidate.objects.filter(pk__in=ids).count(),
            "submitted_count": JobApplication.objects.filter(candidate_id__in=ids, status="submitted").count(),
        }
    if role == "hr":
        company_name = hr_company_name(user)
        company = Company.objects.filter(name__iexact=company_name).first()
        received_qs = JobApplication.objects.filter(status="submitted", company=company) if company else JobApplication.objects.none()
        invite_qs = Invitation.objects.filter(sent_at__isnull=False, application__company=company) if company else Invitation.objects.none()
        return {
            "role": "hr",
            "received_count": received_qs.count(),
            "invite_count": invite_qs.count(),
        }
    return {
        "role": "administration",
        "total_candidates": Candidate.objects.count(),
        "paid_subscriptions": Subscription.objects.filter(paid=True).count(),
        "received_count": JobApplication.objects.filter(status="submitted").count(),
        "feedback_count": Feedback.objects.count(),
    }


def check_email_availability(request):
    email = (request.GET.get("email") or "").strip().lower()
    if not email:
        return JsonResponse({"available": None, "message": "Enter your email address."})
    exists = User.objects.filter(Q(email__iexact=email) | Q(username__iexact=email)).exists()
    return JsonResponse({"available": not exists, "message": "This email is already registered. Please log in." if exists else "Email is available."})


@ensure_csrf_cookie
def login_view(request):
    if request.user.is_authenticated:
        next_url = safe_next_url(request, request.GET.get("next") or request.POST.get("next"))
        return redirect(next_url or role_landing_url(request.user))
    next_url = safe_next_url(request, request.GET.get("next"))
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        next_url = safe_next_url(request, request.POST.get("next"))
        inactive_user = User.objects.filter(email__iexact=email, is_active=False).first()
        if inactive_user:
            onboarding = HROnboardingForm.objects.filter(hr_user=inactive_user).first()
            if onboarding and onboarding.status != "ready":
                messages.error(request, "Your company onboarding is still being processed. You will receive an email when your HR login is ready.")
                return render(request, "core/login.html", {"next": next_url})
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        authenticated = None
        if user:
            authenticated = authenticate(request, username=user.get_username(), password=password)
            if authenticated is None and user.check_password(password):
                authenticated = user
        if authenticated:
            auth_login(request, authenticated)
            request.session.set_expiry(1209600 if request.POST.get("remember") == "true" else 0)
            return redirect(next_url or role_landing_url(authenticated))
        messages.error(request, "Invalid email or password.")
    return render(request, "core/login.html", {"next": next_url})

@role_required("candidate")
def refer_friend(request):
    """Send a simple registration/referral email from a candidate account."""
    if request.method != "POST":
        return redirect("submitted_applications")
    email = (request.POST.get("email") or "").strip().lower()
    from django.core.validators import validate_email
    try:
        validate_email(email)
        subject = "A friend invited you to check Stalwart Business Solutions"
        register_url = request.build_absolute_uri(reverse("signup"))
        body = (
            "Hello,\n\nA friend has referred you to Stalwart Business Solutions. "
            "Please check the platform and register if you are interested.\n\n"
            f"Register here: {register_url}\n\nStalwart Business Solutions"
        )
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
        EmailLog.objects.create(recipient=email, subject=subject, success=True)
        messages.success(request, f"Referral invitation sent to {email}.")
    except ValidationError:
        messages.error(request, "Please enter a valid email address.")
    except Exception as exc:
        EmailLog.objects.create(recipient=email or "unknown@example.com", subject="Friend referral invitation", success=False, error=str(exc))
        messages.error(request, "Referral email could not be sent. Please check the email settings.")
    return redirect("submitted_applications")


def reset_password(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()

        if not email:
            messages.error(request, "Please enter your email address.")
            return render(request, "core/reset-password.html")

        user = User.objects.filter(
            email__iexact=email,
            is_active=True,
        ).first()

        # Always show the same message so we don't reveal
        # whether an email address exists in the system.
        if user:
            token = default_token_generator.make_token(user)

            request.session["password_reset_user_id"] = user.pk
            request.session["password_reset_token"] = token
            request.session["password_reset_email"] = user.email

            reset_url = request.build_absolute_uri(
                reverse(
                    "password_reset_confirm",
                    kwargs={"uid": user.pk, "token": token},
                )
            )

            email_html = render_to_string(
                "core/email-reset-password.html",
                {
                    "reset_url": reset_url,
                    "email": user.email,
                },
                request=request,
            )

            send_mail(
                subject="Reset your Stalwart Business Solutions password",
                message=(
                    "A password reset was requested for your Stalwart Business "
                    "Solutions account.\n\n"
                    f"Reset your password here: {reset_url}"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                html_message=email_html,
                fail_silently=False,
            )

        messages.success(
            request,
            "If an account exists with that email address, "
            "password reset instructions have been sent.",
        )
        return render(request, "core/reset-password.html")

    return render(request, "core/reset-password.html")


def password_reset_confirm(request, uid, token):
    if request.user.is_authenticated:
        return redirect("dashboard")

    try:
        user = User.objects.get(
            pk=uid,
            is_active=True,
        )
    except User.DoesNotExist:
        messages.error(request, "This password reset link is invalid or expired.")
        return redirect("reset_password")

    if not default_token_generator.check_token(user, token):
        messages.error(request, "This password reset link is invalid or expired.")
        return redirect("reset_password")

    if request.method == "POST":
        otp = request.POST.get("otp", "").strip()
        password = request.POST.get("pass", "")
        confirm = request.POST.get("cpass", "")

        # OTP is optional for the first implementation because
        # the secure Django token is already validating the link.
        # If the form sends an OTP, validate it against the session.
        stored_otp = request.session.get("password_reset_otp")

        if stored_otp and otp != stored_otp:
            messages.error(request, "The OTP entered is incorrect.")
            return render(
                request,
                "core/password-set-page.html",
                {"email": user.email},
            )

        if len(password) < 8:
            messages.error(
                request,
                "Password must be at least 8 characters long.",
            )
        elif password != confirm:
            messages.error(
                request,
                "Passwords do not match.",
            )
        else:
            user.set_password(password)
            user.save(update_fields=["password"])

            request.session.pop("password_reset_user_id", None)
            request.session.pop("password_reset_token", None)
            request.session.pop("password_reset_email", None)
            request.session.pop("password_reset_otp", None)

            messages.success(
                request,
                "Your password has been updated successfully. "
                "You can now log in.",
            )
            return redirect("login")

    return render(
        request,
        "core/password-set-page.html",
        {"email": user.email},
    )


@login_required(login_url="/login/")
def logout_view(request):
    # The existing UI uses a normal link, while newer forms may use POST.
    # Support both so logout always works from every page.
    auth_logout(request)
    if request.method == "POST":
        messages.success(request, "You have been logged out successfully.")
    return redirect("login")


def signup(request):
    if request.user.is_authenticated:
        return redirect(role_landing_url(request.user))

    next_url = safe_next_url(request, request.GET.get("next"))
    invitation = None
    account_invitation = None
    invitation_token = request.GET.get("invitation", "").strip()
    account_invite_token = request.GET.get("account_invite", "").strip()

    # Recover an invitation from a candidate application URL.
    if next_url and "/candidate/application/" in next_url:
        token = next_url.rstrip("/").split("/")[-1]
        try:
            invitation = Invitation.objects.select_related(
                "application__candidate",
                "application__company",
            ).get(token=token, active=True)
            invitation_token = str(invitation.token)
        except (Invitation.DoesNotExist, ValueError):
            invitation = None

    if invitation is None and invitation_token:
        invitation = (
            Invitation.objects.select_related(
                "application__candidate",
                "application__company",
            )
            .filter(token=invitation_token, active=True)
            .first()
        )

    if account_invite_token:
        account_invitation = (
            AccountInvitation.objects.select_related("company")
            .filter(token=account_invite_token, active=True)
            .first()
        )

    if request.method == "POST":
        full_name = request.POST.get("address", "").strip()
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("pass", "")
        confirm = request.POST.get("cpass", "")
        next_url = safe_next_url(request, request.POST.get("next"))
        selected_role = request.POST.get("userRole", "jobSeeker").strip()
        employment_upload = request.FILES.get("employment_form")
        extra_uploads = request.FILES.getlist("additional_documents")
        allowed_onboarding_extensions = {".pdf", ".doc", ".docx"}
        upload_error = ""
        for upload in ([employment_upload] if employment_upload else []) + extra_uploads:
            if Path(upload.name).suffix.lower() not in allowed_onboarding_extensions:
                upload_error = "Employment forms and supporting documents must be PDF, DOC or DOCX files."
                break
            if upload.size > 15 * 1024 * 1024:
                upload_error = "Each onboarding file must be 15 MB or smaller."
                break
        if upload_error:
            messages.error(request, upload_error)

        if upload_error:
            pass
        elif account_invitation:
            invited_email = account_invitation.email.strip().lower()
            if email != invited_email:
                messages.error(request, "Please use the email address that received this invitation.")
            elif User.objects.filter(email__iexact=email).exists():
                messages.error(request, "You are an existing user. Please login instead.")
            elif account_invitation.target_role == "hr" and not employment_upload:
                messages.error(request, "Please upload your company's employment form before completing HR registration.")
            elif not full_name or not email or not password:
                messages.error(request, "Please fill in all required fields.")
            elif len(password) < 8:
                messages.error(request, "Password must be at least 8 characters long.")
            elif password != confirm:
                messages.error(request, "Password and Confirm Password don't match.")
            elif request.POST.get("accept") != "true":
                messages.error(request, "Please accept the terms and conditions.")
            else:
                with transaction.atomic():
                    user = User.objects.create_user(
                        username=email, email=email, password=password, first_name=full_name,
                        is_active=(account_invitation.target_role != "hr")
                    )
                    group, _ = Group.objects.get_or_create(name=account_invitation.target_role)
                    user.groups.add(group)
                    profile, _ = UserProfile.objects.get_or_create(user=user)
                    if account_invitation.target_role == "hr" and account_invitation.company:
                        profile.company_name = account_invitation.company.name
                        profile.application_type = ("insitemy" if "insite" in account_invitation.company.name.lower() else "macro_kiosk")
                        profile.save(update_fields=["company_name", "application_type", "updated_at"])
                    if account_invitation.target_role == "candidate":
                        candidate = Candidate.objects.create(user=user, full_name=full_name, email=email)
                        Subscription.objects.get_or_create(candidate=candidate)
                    elif account_invitation.target_role == "hr" and account_invitation.company:
                        from datetime import timedelta
                        onboarding = HROnboardingForm.objects.create(hr_user=user, company_name=account_invitation.company.name, employment_form=employment_upload, implementation_due_at=timezone.now() + timedelta(hours=36), status="pending")
                        for extra_file in extra_uploads:
                            HROnboardingDocument.objects.create(onboarding=onboarding, file=extra_file, original_name=extra_file.name)
                        account_invitation.company.is_active = False
                        account_invitation.company.save(update_fields=["is_active", "updated_at"])
                        admin_users = User.objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(groups__name__in=ROLE_GROUPS["administration"])).distinct()
                        admin_emails = list(admin_users.exclude(email="").values_list("email", flat=True))
                        admin_emails += [item[1] for item in getattr(settings, "ADMINS", []) if len(item) > 1 and item[1]]
                        if getattr(settings, "EDOCS_ADMIN_EMAIL", ""):
                            admin_emails.append(settings.EDOCS_ADMIN_EMAIL)
                        admin_emails = list(dict.fromkeys(admin_emails))
                        action_token = signing.dumps({"onboarding_id": onboarding.pk}, salt="hr-onboarding-email-action")
                        activate_url = request.build_absolute_uri(reverse("admin_onboarding_email_action", args=[action_token, "ready"]))
                        changes_url = request.build_absolute_uri(reverse("admin_onboarding_email_action", args=[action_token, "needs_changes"]))
                        for admin_user in admin_users:
                            Notification.objects.create(recipient=admin_user, title=f"HR form uploaded: {onboarding.company_name}", message=f"{onboarding.company_name} uploaded its employment form. Use the email links to confirm when the form is connected.", target_url=reverse("dashboard"))
                        if admin_emails:
                            try:
                                from django.core.mail import EmailMessage
                                mail = EmailMessage(subject=f"eDocs HR form uploaded: {onboarding.company_name}", body=f"Company: {onboarding.company_name}\nHR: {full_name} ({email})\n\nThe employment form and supporting documents are attached. After the backend form is connected and tested, open this link to select the connected form and activate HR login: {activate_url}\n\nIf the submitted form needs changes, use: {changes_url}\n\nThe HR login activation email is sent immediately after confirmation (normally within a few minutes). This is email-based communication; no admin onboarding page is needed.", from_email=settings.DEFAULT_FROM_EMAIL, to=admin_emails)
                                for upload in [onboarding.employment_form, *[d.file for d in onboarding.documents.all()]]:
                                    try: upload.open("rb"); mail.attach(Path(upload.name).name, upload.read()); upload.close()
                                    except Exception: pass
                                mail.send(fail_silently=False)
                                for recipient in admin_emails: EmailLog.objects.create(recipient=recipient, subject=mail.subject, success=True)
                            except Exception as exc:
                                for recipient in admin_emails: EmailLog.objects.create(recipient=recipient, subject=f"eDocs HR form uploaded: {onboarding.company_name}", success=False, error=str(exc))
                    account_invitation.used_at = timezone.now()
                    account_invitation.active = False
                    account_invitation.save(update_fields=["used_at", "active"])
                if account_invitation.target_role == "hr":
                    messages.success(request, "Company registered successfully. Your employment form has been sent to eDocs admin. HR login will be enabled after the form is implemented.")
                    return redirect("login")
                auth_login(request, user)
                return redirect(next_url or role_landing_url(user))

        elif invitation:
            # An invited candidate can only create a candidate account with
            # the email address to which the invitation was sent.
            invited_email = invitation.application.candidate.email.strip().lower()
            if email != invited_email:
                messages.error(request, "Please use the email address that received this invitation.")
            elif User.objects.filter(email__iexact=email).exists():
                messages.error(request, "You are an existing user. Please login instead.")
            elif not full_name or not email or not password:
                messages.error(request, "Please fill in all required fields.")
            elif len(password) < 8:
                messages.error(request, "Password must be at least 8 characters long.")
            elif password != confirm:
                messages.error(request, "Password and Confirm Password don't match.")
            elif request.POST.get("accept") != "true":
                messages.error(request, "Please accept the terms and conditions.")
            else:
                with transaction.atomic():
                    user = User.objects.create_user(
                        username=email,
                        email=email,
                        password=password,
                        first_name=full_name,
                    )
                    candidate = invitation.application.candidate
                    candidate.user = user
                    candidate.full_name = full_name
                    candidate.email = email
                    candidate.save()
                    Subscription.objects.get_or_create(candidate=candidate)
                    group, _ = Group.objects.get_or_create(name="candidate")
                    user.groups.add(group)
                auth_login(request, user)
                return redirect("candidate_application", token=invitation.token)

        else:
            role_map = {
                "jobSeeker": "candidate",
                "employer": "hr",
                "agency": "agent",
            }
            role = role_map.get(selected_role)
            application_type = request.POST.get("application_type", "").strip().lower()
            company_name = request.POST.get("company_name", "").strip()

            if not role:
                messages.error(request, "Please choose a valid account role.")
            elif role == "hr" and not company_name:
                messages.error(request, "Please enter your company name.")
            elif role == "hr" and not employment_upload:
                messages.error(request, "Please upload your employment application form to complete company onboarding.")
            elif not full_name or not email or not password:
                messages.error(request, "Please fill in all required fields.")
            elif len(password) < 8:
                messages.error(request, "Password must be at least 8 characters long.")
            elif password != confirm:
                messages.error(request, "Password and Confirm Password don't match.")
            elif User.objects.filter(email__iexact=email).exists():
                messages.error(request, "You are an existing user. Please login instead.")
            elif User.objects.filter(username__iexact=email).exists():
                messages.error(request, "An account already exists for this email. Please login instead.")
            elif request.POST.get("accept") != "true":
                messages.error(request, "Please accept the terms and conditions.")
            else:
                with transaction.atomic():
                    user = User.objects.create_user(
                        username=email,
                        email=email,
                        password=password,
                        first_name=full_name,
                        is_active=(role != "hr"),
                    )
                    group, _ = Group.objects.get_or_create(name=role)
                    user.groups.add(group)

                    profile, _ = UserProfile.objects.get_or_create(user=user)
                    if role == "hr":
                        profile.company_name = company_name
                        profile.application_type = application_type if application_type in {"macro_kiosk", "insitemy"} else "macro_kiosk"
                        profile.save(update_fields=["company_name", "application_type", "updated_at"])
                        company, _ = Company.objects.get_or_create(name=company_name, defaults={"email": email, "phone": "", "is_active": False})
                        company.is_active = False
                        company.save(update_fields=["is_active", "updated_at"])
                        from datetime import timedelta
                        onboarding = HROnboardingForm.objects.create(
                            hr_user=user, company_name=company_name, employment_form=employment_upload,
                            implementation_due_at=timezone.now() + timedelta(hours=36), status="pending"
                        )
                        for extra_file in extra_uploads:
                            HROnboardingDocument.objects.create(onboarding=onboarding, file=extra_file, original_name=extra_file.name)
                        admin_users = User.objects.filter(is_active=True).filter(Q(is_superuser=True) | Q(groups__name__in=ROLE_GROUPS["administration"])).distinct()
                        action_token = signing.dumps({"onboarding_id": onboarding.pk}, salt="hr-onboarding-email-action")
                        activate_url = request.build_absolute_uri(reverse("admin_onboarding_email_action", args=[action_token, "ready"]))
                        changes_url = request.build_absolute_uri(reverse("admin_onboarding_email_action", args=[action_token, "needs_changes"]))
                        for admin_user in admin_users:
                            Notification.objects.create(recipient=admin_user, title=f"HR form uploaded: {company_name}", message=f"{company_name} uploaded its employment form. Use the email links to confirm when the form is connected.", target_url=reverse("dashboard"))
                        try:
                            from django.core.mail import EmailMessage
                            admin_emails = list(admin_users.exclude(email="").values_list("email", flat=True))
                            admin_emails += [item[1] for item in getattr(settings, "ADMINS", []) if len(item) > 1 and item[1]]
                            if getattr(settings, "EDOCS_ADMIN_EMAIL", ""):
                                admin_emails.append(settings.EDOCS_ADMIN_EMAIL)
                            admin_emails = list(dict.fromkeys(admin_emails))
                            if admin_emails:
                                mail = EmailMessage(subject=f"eDocs HR form uploaded: {company_name}", body=f"Company: {company_name}\nHR: {full_name} ({email})\n\nThe employment form and supporting documents are attached. After the backend form is connected and tested, open this link to select the connected form and activate HR login: {activate_url}\n\nIf the submitted form needs changes, use: {changes_url}\n\nThe HR login activation email is sent immediately after confirmation (normally within a few minutes). This is email-based communication; no admin onboarding page is needed.", from_email=settings.DEFAULT_FROM_EMAIL, to=admin_emails)
                                uploads = [onboarding.employment_form] + [d.file for d in onboarding.documents.all()]
                                for upload in uploads:
                                    try:
                                        upload.open("rb"); mail.attach(Path(upload.name).name, upload.read()); upload.close()
                                    except Exception:
                                        pass
                                mail.send(fail_silently=False)
                                for recipient in admin_emails: EmailLog.objects.create(recipient=recipient, subject=mail.subject, success=True)
                        except Exception as exc:
                            for recipient in admin_emails: EmailLog.objects.create(recipient=recipient, subject=f"eDocs HR form uploaded: {company_name}", success=False, error=str(exc))

                    if role == "candidate":
                        candidate = Candidate.objects.create(
                            user=user,
                            full_name=full_name,
                            email=email,
                        )
                        Subscription.objects.get_or_create(candidate=candidate)

                if role == "hr":
                    messages.success(request, "Company registered successfully. Your uploaded form has been sent to eDocs admin. You will receive an email when your HR login is ready.")
                    return redirect("login")
                auth_login(request, user)
                return redirect(next_url or role_landing_url(user))

    return render(
        request,
        "core/signup.html",
        {
            "next": next_url,
            "invitation": invitation,
            "invitation_token": invitation_token,
            "account_invitation": account_invitation,
            "account_invite_token": account_invite_token,
        },
    )

def admin_onboarding_email_action(request, token, action):
    """Confirm an onboarding decision from the admin email; no admin onboarding page is required."""
    if action not in {"ready", "needs_changes"}:
        raise Http404("Invalid onboarding action.")
    try:
        payload = signing.loads(token, salt="hr-onboarding-email-action", max_age=60 * 60 * 24 * 30)
        onboarding = HROnboardingForm.objects.select_related("hr_user").get(pk=payload["onboarding_id"])
    except (signing.BadSignature, signing.SignatureExpired, KeyError, HROnboardingForm.DoesNotExist, TypeError):
        return render(request, "core/email-onboarding-action.html", {"error": "This email link is invalid or expired."}, status=400)

    if request.method == "POST":
        if action == "ready":
            slug = (request.POST.get("connected_form_slug") or "").strip().lower()
            if slug not in {"raddish", "nevigate", "macro_kiosk", "insitemy"}:
                messages.error(request, "Choose the connected form before activating HR access.")
            else:
                onboarding.status = "ready"
                onboarding.connected_form_slug = slug
                onboarding.ready_at = timezone.now()
                onboarding.save(update_fields=["status", "connected_form_slug", "ready_at"])
                onboarding.hr_user.is_active = True
                onboarding.hr_user.save(update_fields=["is_active"])
                company, _ = Company.objects.get_or_create(name=onboarding.company_name, defaults={"email": onboarding.hr_user.email, "phone": ""})
                company.is_active = True
                company.save(update_fields=["is_active", "updated_at"])
                Notification.objects.create(recipient=onboarding.hr_user, title="Your HR account is ready", message=f"{onboarding.company_name}'s employment form is connected. You can now log in to eDocs.", target_url=reverse("login"))
                email_sent = False
                try:
                    send_mail(f"{onboarding.company_name} HR account is ready", f"Hello {onboarding.hr_user.first_name},\n\nYour company's employment form has been connected and your HR login is now active. You can log in here: {request.build_absolute_uri(reverse('login'))}\n\nYour Agent must still request and receive your approval before inviting candidates with this form.\n\neDocs", settings.DEFAULT_FROM_EMAIL, [onboarding.hr_user.email], fail_silently=False)
                    EmailLog.objects.create(recipient=onboarding.hr_user.email, subject=f"{onboarding.company_name} HR account is ready", success=True)
                    email_sent = True
                except Exception as exc:
                    EmailLog.objects.create(recipient=onboarding.hr_user.email, subject=f"{onboarding.company_name} HR account is ready", success=False, error=str(exc))
                status_message = (f"{onboarding.company_name} is ready. HR login is active and the HR email has been sent.") if email_sent else (f"{onboarding.company_name} is ready and HR login is active, but the email could not be sent. Check EmailLog and SMTP settings.")
                return render(request, "core/email-onboarding-action.html", {"success": status_message})
        else:
            onboarding.status = "rejected"
            onboarding.save(update_fields=["status"])
            try:
                send_mail(f"Action needed for {onboarding.company_name} onboarding", f"Hello {onboarding.hr_user.first_name},\n\nWe need an update to the employment form you submitted for {onboarding.company_name}. Please reply to the eDocs admin email for details.\n\neDocs", settings.DEFAULT_FROM_EMAIL, [onboarding.hr_user.email], fail_silently=False)
            except Exception:
                pass
            return render(request, "core/email-onboarding-action.html", {"success": "The request was marked as needing changes. The HR contact has been notified."})
    return render(request, "core/email-onboarding-action.html", {"onboarding": onboarding, "action": action})


def agent_hr_email_decision(request, token, action):
    """Allow the HR contact to approve/decline agent access from their email."""
    if action not in {"approve", "deny"}:
        raise Http404("Invalid access decision.")
    try:
        payload = signing.loads(token, salt="agent-hr-access-email", max_age=60 * 60 * 24 * 7)
        link = AgentHRClient.objects.select_related("agent", "hr_user").get(agent_id=payload["agent_id"], hr_user_id=payload["hr_id"])
    except (signing.BadSignature, signing.SignatureExpired, KeyError, AgentHRClient.DoesNotExist, TypeError):
        return render(request, "core/email-onboarding-action.html", {"error": "This access request link is invalid or expired."}, status=400)
    if request.method == "POST":
        requested_status = "approved" if action == "approve" else "denied"
        if link.status in {"approved", "denied"}:
            existing_decision = "approved" if link.status == "approved" else "declined"
            return render(
                request,
                "core/email-onboarding-action.html",
                {"success": f"This request has already been {existing_decision}."},
            )
        link.status = requested_status
        link.save(update_fields=["status", "updated_at"])
        profile = UserProfile.objects.filter(user=link.hr_user).first()
        company_name = (profile.company_name if profile else "") or link.hr_user.email
        decision = "approved" if action == "approve" else "declined"
        Notification.objects.create(recipient=link.agent, title=f"Company access {decision}", message=f"{company_name} {decision} your request to add them as a client.", target_url=reverse("dashboard"))
        try:
            send_mail(f"{company_name} {decision} your eDocs access request", f"Hello {link.agent.first_name or link.agent.email},\n\n{company_name} has {decision} your request. " + ("The company is now available in your Send Invite company dropdown." if action == "approve" else "You cannot send invitations for this company unless HR approves a new request.") + "\n\neDocs", settings.DEFAULT_FROM_EMAIL, [link.agent.email], fail_silently=False)
        except Exception:
            pass
        return render(request, "core/email-onboarding-action.html", {"success": f"You have {decision} the agent's company access request."})
    return render(request, "core/email-onboarding-action.html", {"access_link": link, "action": action})


@role_required("hr")
def manage_agent_client_request(request, pk, action):
    if request.method != "POST" or action not in {"approve", "deny"}: return redirect("dashboard")
    link = get_object_or_404(AgentHRClient, pk=pk, hr_user=request.user)
    link.status = "approved" if action == "approve" else "denied"
    link.save(update_fields=["status", "updated_at"])
    profile = UserProfile.objects.filter(user=request.user).first()
    company_name = (profile.company_name if profile else "") or request.user.email
    Notification.objects.create(recipient=link.agent, title=f"Company access {link.status}", message=f"{company_name} {link.status} your Add Client request.", target_url=reverse("dashboard"))
    try:
        send_mail(f"{company_name} {link.status} your eDocs access request", f"Hello {link.agent.first_name or link.agent.email},\n\n{company_name} {link.status} your request to add them as a client.\n\neDocs", settings.DEFAULT_FROM_EMAIL, [link.agent.email], fail_silently=True)
    except Exception:
        pass
    messages.success(request, f"Agent access {link.status}.")
    return redirect("dashboard")


@login_required(login_url="/login/")
def dashboard(request):
    role = get_role(request.user)
    metrics = dashboard_metrics(request.user)
    app_type = user_application_type(request.user) if role == "hr" else ""
    context = {
        "role": role,
        **metrics,
        "hr_application_type": app_type,
        "hr_company_name": (getattr(getattr(request.user, "profile", None), "company_name", "") or ("InsiteMy" if app_type == "insitemy" else "Macro Kiosk" if app_type == "macro_kiosk" else "")),
        "hr_onboarding_form": HROnboardingForm.objects.filter(hr_user=request.user).first() if role == "hr" else None,
        "pending_agent_requests": AgentHRClient.objects.filter(hr_user=request.user, status="pending").select_related("agent") if role == "hr" else [],
        "admin_onboarding_count": HROnboardingForm.objects.exclude(status="ready").count() if role == "administration" else 0,
        "notifications": Notification.objects.filter(
            recipient=request.user, is_read=False
        ).order_by("-created_at")[:8],
    }
    return render(request, "core/index.html", context)


@login_required(login_url="/login/")
def open_notification(request, pk):
    notification = get_object_or_404(
        Notification,
        pk=pk,
        recipient=request.user,
    )
    notification.is_read = True
    notification.save(update_fields=["is_read"])
    return redirect(notification.target_url or "dashboard")


@login_required(login_url="/login/")
def mark_notifications_read(request):
    if request.method == "POST":
        Notification.objects.filter(
            recipient=request.user, is_read=False
        ).update(is_read=True)
    return redirect(request.POST.get("next") or "dashboard")


@login_required(login_url="/login/")
def dashboard_data(request):
    return JsonResponse(dashboard_metrics(request.user), safe=True)


@role_required("hr", "administration")
def company_page(request, company_name):
    company_name = company_name.strip().lower()
    if company_name not in {"insitemy", "macro kiosk"}:
        raise Http404("Application workflow not found.")

    if get_role(request.user) == "hr":
        expected = user_application_type(request.user)
        if (company_name == "insitemy" and expected != "insitemy") or (
            company_name == "macro kiosk" and expected != "macro_kiosk"
        ):
            return redirect("insitemy" if expected == "insitemy" else "macro_kiosk")

    display_name = "InsiteMy" if company_name == "insitemy" else "Macro Kiosk"
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    return render(
        request,
        "core/hr-invite.html",
        {
            "hr_entry": True,
            "company": display_name,
            "application_type": company_name.replace(" ", "_"),
            "hr_profile": profile,
        },
    )


@role_required("hr", "administration")
def insitemy(request):
    return company_page(request, "InsiteMy")


@role_required("hr", "administration")
def macro_kiosk(request):
    return company_page(request, "Macro Kiosk")





def _audit_event(request, app, action):
    """Record a business-workflow event for the application audit timeline."""
    if not app:
        return
    ip_address = request.META.get("REMOTE_ADDR") or None
    AuditTrail.objects.create(
        application=app,
        user=request.user if getattr(request, "user", None) and request.user.is_authenticated else None,
        action=action,
        ip_address=ip_address,
        location=_ip_location(ip_address) if ip_address else "",
    )


def _send_account_invitation_email(request, invitation):
    """Send an account invitation for HR/Agent/Candidate onboarding."""
    email = invitation.email
    target = invitation.get_target_role_display()
    company_name = invitation.company.name if invitation.company else ""
    signup_url = request.build_absolute_uri(
        reverse("signup") + "?account_invite=" + str(invitation.token)
    )
    login_url = request.build_absolute_uri(reverse("login"))
    if invitation.target_role == "candidate" and invitation.company:
        subject = f"{company_name} - Application Invitation"
        purpose = (
            f"You have been invited to use Stalwart Business Solutions to apply "
            f"for {company_name}. Please register or log in to continue with the application."
        )
    elif invitation.target_role == "hr":
        subject = "Stalwart Business Solutions - HR Account Invitation"
        purpose = (
            "You have been invited to register as an HR user. "
            "This account will be connected to the assigned company so you can access its applications."
        )
    else:
        subject = "Stalwart Business Solutions - Account Invitation"
        purpose = (
            f"You have been invited to register as a {target.lower()} on Stalwart Business Solutions."
        )

    body = (
        f"Dear {email},\n\n{purpose}\n\n"
        f"If you already have an account, please log in here:\n{login_url}\n\n"
        f"If you do not have an account, register here:\n{signup_url}\n\n"
        "Stalwart Business Solutions"
    )
    try:
        send_mail(
            subject, body, settings.DEFAULT_FROM_EMAIL, [email],
            fail_silently=False,
        )
        invitation.sent_at = timezone.now()
        invitation.save(update_fields=["sent_at"])
        EmailLog.objects.create(recipient=email, subject=subject, success=True)
        return True
    except Exception as exc:
        EmailLog.objects.create(recipient=email, subject=subject, success=False, error=str(exc))
        return False



def _send_invitation_email(request, invitation):
    app = invitation.application
    candidate = app.candidate

    link = request.build_absolute_uri(
        reverse(
            "candidate_invitation",
            kwargs={"token": invitation.token},
        )
    )

    sender_name = (
        request.user.get_full_name()
        or request.user.email
        or "HR Team"
    )

    subject = f"{app.company.name} - Job Application Invitation"

    body = (
        f"Dear {candidate.full_name or 'Candidate'},\n\n"
        f"{sender_name} from {app.company.name} has invited you "
        "to complete your job application.\n\n"
        "Secure application link:\n"
        f"{link}\n\n"
        "Please do not share this link with anyone.\n\n"
        "Stalwart Business Solutions"
    )

    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [candidate.email],
            fail_silently=False,
        )

        EmailLog.objects.create(
            recipient=candidate.email,
            subject=subject,
            success=True,
        )

        invitation.sent_at = timezone.now()
        invitation.save(update_fields=["sent_at"])

        app.status = "invited"
        app.save(update_fields=["status", "updated_at"])

        return True

    except Exception as exc:
        EmailLog.objects.create(
            recipient=candidate.email,
            subject=subject,
            success=False,
            error=str(exc),
        )

        return False




@role_required("hr", "administration", "agent", "candidate")
def send_invite_page(request):
    role = get_role(request.user)
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    # HR invitations are company-scoped by the HR account.  The company can
    # also be supplied by the Job Application > Send an Invite link.
    company_key = (request.GET.get("company") or "").strip().lower()
    if role == "hr":
        company_name = hr_company_name(request.user)
        company_key = "insitemy" if company_name.lower() == "insitemy" else "macro_kiosk"
    else:
        selected_company = Company.objects.filter(name__iexact=company_key, is_active=True).first() if company_key else None
        company_name = selected_company.name if selected_company else (
            "InsiteMy" if company_key == "insitemy"
            else "Macro Kiosk" if company_key in {"macro_kiosk", "macro kiosk"}
            else ""
        )
        if selected_company:
            company_key = selected_company.name
    company_options = []
    if role == "administration":
        company_options = [(company.name, company.name) for company in Company.objects.filter(is_active=True, archived=False).order_by("name")]
    elif role == "agent":
        approved = AgentHRClient.objects.filter(agent=request.user, status="approved", hr_user__is_active=True).select_related("hr_user", "hr_user__profile", "hr_user__onboarding_form")
        for link in approved:
            try:
                onboarding = link.hr_user.onboarding_form
            except HROnboardingForm.DoesNotExist:
                onboarding = None
            profile_name = (link.hr_user.profile.company_name if hasattr(link.hr_user, "profile") else "")
            # HR approval controls whether the company is selectable. Show the
            # approved HR company's registered name immediately; form setup can
            # be completed independently.
            display_name = (
                onboarding.company_name
                if onboarding and onboarding.company_name
                else profile_name
            ) or link.hr_user.email
            company_options.append((display_name, display_name))

    return render(request, "core/send-invite.html", {
        "role": role,
        "profile": profile,
        "company_name": company_name,
        "company_key": company_key,
        "company_options": company_options,
    })


@role_required("hr", "administration", "agent", "candidate")
def send_invitation(request):
    """Unified invitation sender for HR, agents and candidates."""
    if request.method != "POST":
        return redirect(role_landing_url(request.user))

    sender_role = get_role(request.user)
    recipient_role = request.POST.get("recipient_role", "candidate").strip().lower()
    raw_company = request.POST.get("company", "").strip().lower()
    raw_emails = request.POST.get("emails", "") or request.POST.get("email", "")
    emails = list(dict.fromkeys(
        email.strip().lower()
        for email in re.split(r"[,;\s]+", raw_emails)
        if email.strip()
    ))
    name = request.POST.get("name", "").strip()
    phone = request.POST.get("phone", "").strip()

    allowed_targets = {
        "hr": {"candidate", "agent"},
        "agent": {"candidate", "hr"},
        "candidate": {"hr", "agent"},
        "administration": {"candidate", "agent", "hr"},
    }
    if recipient_role not in allowed_targets.get(sender_role, set()):
        messages.error(request, "That invitation type is not available for your account.")
        return redirect(role_landing_url(request.user))
    if not emails:
        messages.error(request, "Please enter at least one email address.")
        return redirect(role_landing_url(request.user))

    from django.core.validators import validate_email
    for email in emails:
        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, f"Invalid email address: {email}")
            return redirect(role_landing_url(request.user))

    # An agent inviting a new HR client supplies the company's name.
    if sender_role == "agent" and recipient_role == "hr":
        client_company_name = (request.POST.get("client_company_name") or name).strip()
        if not client_company_name:
            messages.error(request, "Please enter the company name.")
            return redirect(role_landing_url(request.user))
        company, _ = Company.objects.get_or_create(name=client_company_name, defaults={"email": emails[0], "phone": ""})
    # HR accounts are permanently scoped to their registered company.
    elif sender_role == "hr":
        onboarding = HROnboardingForm.objects.filter(hr_user=request.user, status="ready").exclude(connected_form_slug="").first()
        if onboarding:
            company, _ = Company.objects.get_or_create(name=onboarding.company_name, defaults={"email": request.user.email, "phone": ""})
        else:
            application_type = user_application_type(request.user)
            company = get_insitemy_company() if application_type == "insitemy" else get_macro_kiosk_company()
    elif sender_role == "administration" and recipient_role == "candidate":
        requested_company = (request.POST.get("company") or "").strip()
        company = Company.objects.filter(name__iexact=requested_company, is_active=True).first()
        if company is None:
            messages.error(request, "Please select an active company from Job Application.")
            return redirect("send_invite_page")
    elif sender_role == "agent" and recipient_role == "candidate":
        approved_links = AgentHRClient.objects.filter(
            agent=request.user,
            status="approved",
            hr_user__is_active=True,
        ).select_related("hr_user", "hr_user__profile", "hr_user__onboarding_form")
        requested_name = (request.POST.get("company_name") or request.POST.get("company") or "").strip()
        chosen = None
        if requested_name:
            chosen = next((link for link in approved_links if (getattr(getattr(link.hr_user, "profile", None), "company_name", "") or "").casefold() == requested_name.casefold()), None)
        if chosen is None and not requested_name:
            chosen = approved_links.first()
        if chosen:
            try:
                connected_name = chosen.hr_user.onboarding_form.company_name
            except HROnboardingForm.DoesNotExist:
                connected_name = ""
            connected_name = connected_name or (
                getattr(getattr(chosen.hr_user, "profile", None), "company_name", "")
                or chosen.hr_user.email
            )
            company, _ = Company.objects.get_or_create(
                name=connected_name,
                defaults={"email": chosen.hr_user.email, "phone": ""},
            )
        else:
            messages.error(request, "Please select a company approved by HR.")
            return redirect("send_invite_page")
    else:
        sender_type = user_application_type(request.user)
        company = get_insitemy_company() if sender_type == "insitemy" else get_macro_kiosk_company()

    if recipient_role in {"candidate", "hr"} and company is None:
        messages.error(request, "The application company could not be resolved for this account.")
        return redirect(role_landing_url(request.user))

    if recipient_role == "candidate":
        if company is None:
            messages.error(request, "Please select the company application to send.")
            return redirect(role_landing_url(request.user))

        sent = failed = 0
        for email in emails:
            try:
                with transaction.atomic():
                    candidate = Candidate.objects.filter(email__iexact=email).order_by("id").first()
                    if not candidate:
                        candidate = Candidate.objects.create(email=email, full_name=name, phone=phone)
                    else:
                        changed = False
                        if name and not candidate.full_name:
                            candidate.full_name = name; changed = True
                        if phone and not candidate.phone:
                            candidate.phone = phone; changed = True
                        if changed:
                            candidate.save()

                    Subscription.objects.get_or_create(candidate=candidate)

                    if sender_role == "agent":
                        AgentClient.objects.get_or_create(
                            agent=request.user,
                            candidate=candidate,
                        )

                    Invitation.objects.filter(
                        application__candidate=candidate,
                        application__company=company,
                        active=True,
                    ).update(active=False)
                    application = JobApplication.objects.create(
                        candidate=candidate, company=company, status="draft", data={}
                    )
                    invitation = Invitation.objects.create(
                        application=application, invited_by=request.user
                    )

                # Existing users receive the same secure application link, with
                # a clear login/register instruction in the email.
                link = request.build_absolute_uri(
                    reverse("candidate_invitation", kwargs={"token": invitation.token})
                )
                login_url = request.build_absolute_uri(reverse("login"))
                signup_url = request.build_absolute_uri(
                    reverse("signup") + "?invitation=" + str(invitation.token)
                )
                subject = f"{company.name} - Job Application Invitation"
                body = (
                    f"Dear {candidate.full_name or 'Candidate'},\n\n"
                    f"{request.user.get_full_name() or request.user.email} has invited you "
                    f"to apply for {company.name} using Stalwart Business Solutions.\n\n"
                    "If you already have an account, log in first and then open the application:\n"
                    f"{login_url}\n{link}\n\n"
                    "If you do not have an account, register here:\n"
                    f"{signup_url}\n\nStalwart Business Solutions"
                )
                try:
                    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
                    invitation.sent_at = timezone.now()
                    invitation.save(update_fields=["sent_at"])
                    application.status = "invited"
                    application.save(update_fields=["status", "updated_at"])
                    EmailLog.objects.create(recipient=email, subject=subject, success=True)
                    _audit_event(request, application, "invite_sent")
                    sent += 1
                except Exception as exc:
                    EmailLog.objects.create(recipient=email, subject=subject, success=False, error=str(exc))
                    failed += 1
            except Exception as exc:
                failed += 1
                EmailLog.objects.create(
                    recipient=email, subject=f"{company.name} - Job Application Invitation",
                    success=False, error=str(exc)
                )
        if sent:
            messages.success(request, f"{sent} invitation(s) sent successfully.")
        if failed:
            messages.error(request, f"{failed} invitation(s) failed. Open EmailLog after fixing SMTP settings for the exact error.")
        return redirect(role_landing_url(request.user))

    # HR/Agent account invitation.
    sent = failed = 0
    for email in emails:
        existing = AccountInvitation.objects.filter(
            email__iexact=email, target_role=recipient_role, active=True
        ).first()
        if existing:
            existing.active = False
            existing.save(update_fields=["active"])
        invitation = AccountInvitation.objects.create(
            email=email, invited_by=request.user,
            target_role=recipient_role, company=company
        )
        if _send_account_invitation_email(request, invitation):
            sent += 1
        else:
            failed += 1
    if sent:
        messages.success(request, f"{sent} account invitation(s) sent successfully.")
    if failed:
        messages.error(request, f"{failed} invitation(s) failed. Check EmailLog for the SMTP error.")
    return redirect(role_landing_url(request.user))


@role_required("agent")
def search_agent_clients(request):
    """Search registered HR/company accounts for an agent's Add Client flow."""
    query = request.GET.get("q", "").strip()
    if len(query) < 1:
        return JsonResponse({"results": []})
    hr_group = Group.objects.filter(name__iexact="hr").first()
    users = User.objects.filter(Q(profile__company_name__icontains=query) | Q(first_name__icontains=query) | Q(email__icontains=query))
    if hr_group:
        users = users.filter(groups=hr_group)
    users = users.select_related("profile", "onboarding_form").distinct().order_by("profile__company_name", "id")[:15]
    results = []
    for user in users:
        try:
            profile = user.profile
        except UserProfile.DoesNotExist:
            profile = None
        results.append({
            "id": user.id,
            "name": (profile.company_name if profile else "") or user.get_full_name() or user.email,
            "email": user.email,
        })
    return JsonResponse({"results": results})


@role_required("agent")
def add_agent_client(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST request required."}, status=405)
    client_id = request.POST.get("client_id", "").strip() or request.POST.get("candidate_id", "").strip()
    if not client_id.isdigit():
        return JsonResponse({"success": False, "message": "Please select a registered HR client."}, status=400)
    client = get_object_or_404(User, pk=int(client_id))
    if get_role(client) != "hr":
        return JsonResponse({"success": False, "message": "This account is not an HR client."}, status=400)
    # Create an access request only. HR must explicitly approve it before
    # the company is available in the agent's Send Invite dropdown.
    profile = UserProfile.objects.filter(user=client).first()
    onboarding = HROnboardingForm.objects.filter(hr_user=client).first()
    link, created = AgentHRClient.objects.get_or_create(
        agent=request.user,
        hr_user=client,
        defaults={"status": "pending"},
    )
    if not created and link.status == "denied":
        link.status = "pending"
        link.save(update_fields=["status", "updated_at"])
        created = True
    if created:
        company_label = (
            onboarding.company_name if onboarding
            else (profile.company_name if profile else "")
        ) or client.email
        Notification.objects.create(recipient=client, title="Agent requests company access", message=f"{request.user.get_full_name() or request.user.email} requested permission to add {company_label} as a client.", target_url=reverse("dashboard"))
        try:
            decision_token = signing.dumps({"agent_id": request.user.pk, "hr_id": client.pk}, salt="agent-hr-access-email")
            approve_url = request.build_absolute_uri(reverse("agent_hr_email_decision", args=[decision_token, "approve"]))
            deny_url = request.build_absolute_uri(reverse("agent_hr_email_decision", args=[decision_token, "deny"]))
            agent_name = request.user.get_full_name() or request.user.email
            subject = f"Company access request from {agent_name}"
            body = (f"Hello {client.first_name or client.email},\n\n{agent_name} ({request.user.email}) requests access to {company_label} so they can send job application invitations for your company.\n\nApprove: {approve_url}\n\nDecline: {deny_url}\n\nThese links expire after 7 days. If you did not expect this request, decline it.\n\neDocs")
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [client.email], fail_silently=False)
            EmailLog.objects.create(recipient=client.email, subject=subject, success=True)
        except Exception as exc:
            EmailLog.objects.create(recipient=client.email or "unknown@example.com", subject="Company access request", success=False, error=str(exc))
            return JsonResponse({"success": False, "message": "Access request was recorded, but the email could not be sent. Check SMTP settings."}, status=500)
    return JsonResponse({"success": True, "message": "Access request email sent to HR for approval." if created else ("Client access is already approved." if link.status == "approved" else "Your access request is already pending.")})


@role_required("agent")
def invite_agent_client(request):
    """Send a purpose-specific eDocs invitation to a new client HR contact."""
    if request.method != "POST":
        return JsonResponse({"success": False, "message": "POST request required."}, status=405)
    company_name = (request.POST.get("company_name") or "").strip()
    email = (request.POST.get("email") or "").strip().lower()
    from django.core.validators import validate_email
    if not company_name:
        return JsonResponse({"success": False, "message": "Enter the company name."}, status=400)
    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({"success": False, "message": "Enter a valid HR email address."}, status=400)

    existing_user = User.objects.filter(email__iexact=email).first()
    if existing_user:
        if get_role(existing_user) != "hr":
            return JsonResponse({"success": False, "message": "This email is already registered under another account type."}, status=409)
        link, created = AgentHRClient.objects.get_or_create(agent=request.user, hr_user=existing_user, defaults={"status": "pending"})
        if not created and link.status == "denied":
            link.status = "pending"
            link.save(update_fields=["status", "updated_at"])
            created = True
        if created:
            profile = UserProfile.objects.filter(user=existing_user).first()
            company_label = (profile.company_name if profile else "") or existing_user.email
            Notification.objects.create(recipient=existing_user, title="Agent requests company access", message=f"{request.user.get_full_name() or request.user.email} requested permission to add {company_label} as a client.", target_url=reverse("dashboard"))
            try:
                decision_token = signing.dumps({"agent_id": request.user.pk, "hr_id": existing_user.pk}, salt="agent-hr-access-email")
                approve_url = request.build_absolute_uri(reverse("agent_hr_email_decision", args=[decision_token, "approve"]))
                deny_url = request.build_absolute_uri(reverse("agent_hr_email_decision", args=[decision_token, "deny"]))
                agent_name = request.user.get_full_name() or request.user.email
                subject = f"Company access request from {agent_name}"
                body = f"Hello {existing_user.first_name or existing_user.email},\n\n{agent_name} ({request.user.email}) requests access to {company_label} so they can send job application invitations for your company.\n\nApprove: {approve_url}\n\nDecline: {deny_url}\n\nThese links expire after 7 days.\n\neDocs"
                send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [existing_user.email], fail_silently=False)
                EmailLog.objects.create(recipient=existing_user.email, subject=subject, success=True)
            except Exception as exc:
                EmailLog.objects.create(recipient=existing_user.email, subject="Company access request", success=False, error=str(exc))
                return JsonResponse({"success": False, "message": "Access request was recorded, but the email could not be sent. Check SMTP settings."}, status=500)
            return JsonResponse({"success": True, "message": f"Access request email sent to the existing HR account {existing_user.email}."})
        return JsonResponse({"success": True, "message": "Access is already approved." if link.status == "approved" else "An access request is already pending."})

    company, _ = Company.objects.get_or_create(
        name=company_name,
        defaults={"email": email, "phone": ""},
    )
    invitation = AccountInvitation.objects.create(
        email=email, invited_by=request.user, target_role="hr", company=company, active=True
    )
    invite_link = request.build_absolute_uri(reverse("signup") + "?account_invite=" + str(invitation.token))
    profile = UserProfile.objects.filter(user=request.user).first()
    agency = (profile.company_name if profile else "") or request.user.get_full_name() or request.user.username
    subject = f"{agency} has invited you to join eDocs"
    body = (
        f"Hello {company_name},\n\n"
        f"{agency} has invited you to join eDocs, a secure way to share and manage candidate’s documents.\n\n"
        f"Join here: {invite_link}\n\n"
        "If you already have an account, please log in to continue.\n\neDocs"
    )
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [email], fail_silently=False)
        invitation.sent_at = timezone.now()
        invitation.save(update_fields=["sent_at"])
        EmailLog.objects.create(recipient=email, subject=subject, success=True)
    except Exception as exc:
        EmailLog.objects.create(recipient=email, subject=subject, success=False, error=str(exc))
        invitation.active = False
        invitation.save(update_fields=["active"])
        return JsonResponse({"success": False, "message": "The invitation email could not be sent. Check your email/SMTP settings and try again."}, status=500)
    return JsonResponse({"success": True, "message": f"Invitation sent to {email}."})


@login_required(login_url="/login/")
@role_required("hr")
def upload_hr_employment_form(request):
    if request.method != "POST":
        return redirect("dashboard")
    uploaded = request.FILES.get("employment_form")
    if not uploaded:
        messages.error(request, "Please choose an employment form to upload.")
        return redirect("dashboard")
    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    company_name = (profile.company_name or request.POST.get("company_name", "")).strip()
    if not company_name:
        messages.error(request, "Please add your company name to your profile first.")
        return redirect("dashboard")
    from datetime import timedelta
    form, created = HROnboardingForm.objects.get_or_create(hr_user=request.user, defaults={"company_name": company_name, "employment_form": uploaded, "implementation_due_at": timezone.now() + timedelta(hours=36)})
    if not created:
        form.company_name = company_name
        form.employment_form = uploaded
        form.uploaded_at = timezone.now()
        form.implementation_due_at = timezone.now() + timedelta(hours=36)
        form.save()
    # Notify the agent who invited this HR account, when applicable.
    inviter = AccountInvitation.objects.filter(email__iexact=request.user.email, target_role="hr", invited_by__isnull=False).order_by("-id").first()
    if inviter and inviter.invited_by and inviter.invited_by.email:
        subject = f"{company_name} uploaded its employment form"
        body = (f"Hello,\n\n{company_name} has uploaded its employment form and completed the initial onboarding step successfully. A copy of the form is attached.\nThe form is expected to be implemented within 36 hours.\n\nStalwart Business Solutions")
        try:
            form.employment_form.open("rb")
            try:
                attachment_data = form.employment_form.read()
            finally:
                form.employment_form.close()
            filename = Path(form.employment_form.name).name
            content_type = getattr(uploaded, "content_type", "application/octet-stream")
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [inviter.invited_by.email], fail_silently=False, attachments=[(filename, attachment_data, content_type)])
            EmailLog.objects.create(recipient=inviter.invited_by.email, subject=subject, success=True)
        except Exception as exc:
            EmailLog.objects.create(recipient=inviter.invited_by.email, subject=subject, success=False, error=str(exc))
    messages.success(request, "Employment form uploaded. The agent who invited you has been notified.")
    return redirect("dashboard")


def candidate_invitation(request, token):
    invitation = get_object_or_404(Invitation.objects.select_related("application__candidate"), token=token, active=True)
    app = invitation.application
    if request.user.is_authenticated:
        role = get_role(request.user)
        if role != "candidate":
            messages.error(request, "This invitation is for a candidate account.")
            return redirect("dashboard")
        candidate = candidate_for_user(request.user)
        if not candidate or candidate.pk != app.candidate_id:
            messages.error(request, "Please use the candidate account associated with this invitation.")
            return redirect("dashboard")
        return redirect("candidate_application", token=invitation.token)
    next_url = reverse("candidate_application", kwargs={"token": invitation.token})
    return render(request, "core/candidate_invitation.html", {"invitation": invitation, "application": app, "company": app.company, "next": next_url})


def _section_from_request(request):
    section = (request.POST.get("save_section") or request.POST.get("section") or "").strip().lower()
    aliases = {
        "personal": "personal_information", "personal_info": "personal_information", "address": "personal_information",
        "iq": "iq_test", "documents_submission": "documents", "employment": "employment_expectations",
        "expectations": "employment_expectations", "references": "reference", "declaration_form": "declaration",
    }
    return aliases.get(section, section)


def _collect_post_data(request):
    data = {}
    for key, values in request.POST.lists():
        if key in IGNORED_POST_FIELDS:
            continue
        key = key.strip()
        if not key:
            continue
        # ``name`` appears in a few consent/signature blocks in the legacy
        # Macro Kiosk HTML. They all used the same POST name, so Django receives
        # a list such as ["Buvana", "Buvana", "Buvana"]. The candidate's
        # identity is the first non-empty value; do not store the duplicate
        # consent-field values as the candidate name.
        if key == "name":
            first_value = next((v for v in values if str(v).strip()), "")
            data[key] = normalize_person_name(first_value)
        else:
            data[key] = values[0] if len(values) == 1 else values
    return data


def _section_field_names(section, request):
    keys = set(request.POST.keys()) - IGNORED_POST_FIELDS
    if section == "personal_information":
        exact = {
            "position_applied", "others", "name", "passportnumber", "nationality", "dob", "age",
            "gender", "marital_status", "marital", "height", "weight", "bms", "email", "phone",
            "medical_condition", "medical_condition_details", "criminal_offence", "criminal_offence_details",
            "same_as_residential",
        }
        prefixes = ("residential_", "permanent_")
    elif section == "iq_test":
        exact, prefixes = set(), ("iq_",)
    elif section == "consent":
        exact, prefixes = {"consentname", "appname", "nric", "date"}, ("consent_attachment_",)
    elif section == "documents":
        exact, prefixes = set(), (
            "personality_test_result", "updated_resume", "identity_document",
            "academic_certificate", "payslips",
        )
    elif section == "employment_expectations":
        exact = {
            "willing_to_travel", "willing_to_relocate", "own_transport",
            "lastsalary", "lastsalary_2", "expectedsalary", "notice_period", "stalwart_referral",
        }
        prefixes = ()
    elif section == "reference":
        exact = {
            "ref1name", "ref1pos", "ref1com", "ref1email", "ref1phn",
            "ref2name", "ref2pos", "ref2com", "ref2email", "ref2phn",
        }
        prefixes = ()
    elif section == "declaration":
        exact = {"declaration_appname", "appplace", "dateapplied"}
        prefixes = ("applicant_signature",)
    elif section.startswith("macro_section_"):
        macro_fields = {
            "macro_section_1": {
                "macro_1_checkbox_1","macro_1_checkbox_2","macro_1_checkbox_3","macro_1_checkbox_4",
                "macro_1_checkbox_5","macro_1_checkbox_6","macro_1_checkbox_7","macro_1_checkbox_8",
                "macro_1_checkbox_9","macro_1_checkbox_10","macro_1_checkbox_11","macro_1_checkbox_12",
                "others","others_2","macro_doc_1_1","name","address","address_2","dob","age","gender",
                "dob_2","email","passportnumber","marital","nationality","nationality_2","nationality_3",
                "nationality_4","father","mother","spouse","children","brothers","sisters"
            },
            "macro_section_2": {
                "datefrom","dateto","height","location","superior","position","ssalary","esalary",
                "reasons","macro_2_text_1"
            },
            "macro_section_3": {
                "height","location","yearfrom","yearto","qualification","height_2","location_2",
                "yearfrom_2","yearto_2","qualification_2","height_3","height_4","height_5",
                "lang","lang_2","lang_3","lang_4","lang_5","lang_6","lang_7","lang_8","lang_9",
                "lang_10","lang_11","lang_12","lang_13","lang_14","lang_15","lang_16","lang_17","lang_18",
                "others","others_2"
            },
            "macro_section_4": {
                "macro_4_checkbox_1","macro_4_checkbox_2","macro_4_checkbox_3","macro_4_checkbox_4",
                "macro_4_checkbox_5","macro_4_checkbox_6","macro_4_checkbox_7","macro_4_checkbox_8",
                "macro_4_checkbox_9","macro_4_checkbox_10","macro_4_checkbox_11","macro_4_checkbox_12",
                "macro_4_checkbox_13","macro_4_checkbox_14","macro_4_checkbox_15","macro_4_checkbox_16",
                "macro_4_checkbox_17","macro_4_checkbox_18","macro_4_checkbox_19","macro_4_checkbox_20",
                "elaborate","elaborate_2","elaborate_3","elaborate_4","elaborate_5","elaborate_6",
                "elaborate_7","elaborate_8","elaborate_9","elaborate_10","lastsalary","lastsalary_2",
                "expectedsalary","ref1name","ref1pos","ref1com","ref1email","ref1phn",
                "ref1name_2","ref1pos_2","ref1com_2","ref1email_2","ref1phn_2","appname","dateapplied"
            },
            "macro_section_5": {"macro_5_yourname","macro_5_nric","macro_5_name","macro_5_date","macro_doc_5_1","name","date","nric","macro_5_consent"},
            "macro_section_6": {"macro_6_text_1"},
            "macro_section_7": {"macro_7_name","macro_7_nric","macro_7_date","name","nric","date","macro_7_consent"},
        }
        exact, prefixes = macro_fields.get(section, set()), ()
    else:
        exact, prefixes = keys, ()
    return {
        key for key in keys
        if key in exact or any(key.startswith(prefix) for prefix in prefixes)
    }



def _save_uploaded_file(app, field_name, uploaded_file):
    if uploaded_file.size > MAX_UPLOAD_SIZE:
        raise ValidationError(f"{uploaded_file.name} is larger than 1 MB.")
    ext = Path(uploaded_file.name).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValidationError(f"File type {ext or 'unknown'} is not allowed.")
    return ApplicationFile.objects.create(application=app, file=uploaded_file, field_name=field_name)


def _merge_section(saved_data, section, data):
    if section:
        old = saved_data.get(section)
        if not isinstance(old, dict):
            old = {}
        old.update(data)
        saved_data[section] = old
    else:
        saved_data.update(data)
    return saved_data


def _candidate_defaults(app):
    """Return only safe, genuinely common identity defaults for a new form."""
    candidate = app.candidate
    saved = deepcopy(app.data) if isinstance(app.data, dict) else {}

    common_fields = {
        "name", "email", "phone", "passportnumber", "dob", "gender",
        "nationality", "marital", "marital_status",
    }

    previous = (
        JobApplication.objects
        .filter(
            candidate=candidate,
            status__in={"submitted", "reviewed", "shortlisted", "rejected"},
        )
        .exclude(pk=app.pk)
        .order_by("-submitted_at", "-updated_at")
        .first()
    )

    previous_common = {}
    if previous and isinstance(previous.data, dict):
        for section_name in ("personal_information", "macro_section_1"):
            section = previous.data.get(section_name, {})
            if isinstance(section, dict):
                for key in common_fields:
                    value = section.get(key)
                    if value not in ("", None, [], {}):
                        previous_common[key] = deepcopy(value)

    def set_if_missing(target, key, value):
        if key not in target or target.get(key) in ("", None, [], {}):
            target[key] = deepcopy(value)

    if is_insitemy_application(app):
        personal = saved.get("personal_information")
        if not isinstance(personal, dict):
            personal = {}
        for key, value in previous_common.items():
            set_if_missing(personal, key, value)
        set_if_missing(personal, "name", normalize_person_name(candidate.full_name))
        set_if_missing(personal, "email", candidate.email or "")
        set_if_missing(personal, "phone", candidate.phone or "")
        saved["personal_information"] = personal

    elif is_macro_kiosk_application(app):
        macro = saved.get("macro_section_1")
        if not isinstance(macro, dict):
            macro = {}
        for key, value in previous_common.items():
            if key != "phone":
                set_if_missing(macro, key, value)
        set_if_missing(macro, "name", normalize_person_name(candidate.full_name))
        set_if_missing(macro, "email", candidate.email or "")
        saved["macro_section_1"] = macro

    return saved


MACRO_REQUIRED_FIELDS = {
    "macro_section_1": (
        "name", "address", "address_2", "dob", "age", "gender", "dob_2",
        "email", "passportnumber", "marital", "nationality", "nationality_4",
        "father", "mother", "spouse", "children", "brothers", "sisters",
    ),
    "macro_section_2": (
        "datefrom", "dateto", "height", "location", "superior", "position",
        "ssalary", "esalary", "reasons",
    ),
    "macro_section_3": ("yearfrom", "yearto", "qualification"),
    "macro_section_4": (
        "lastsalary_2", "expectedsalary", "ref1name", "ref1pos", "ref1com",
        "ref1email", "ref1phn", "ref1name_2", "ref1pos_2", "ref1com_2",
        "ref1email_2", "ref1phn_2", "appname", "dateapplied",
    ),
    "macro_section_5": ("macro_5_yourname", "macro_5_nric", "macro_5_name", "macro_5_date", "nric", "macro_5_consent"),
    "macro_section_6": ("macro_6_text_1",),
    "macro_section_7": ("macro_7_name", "macro_7_nric", "macro_7_date", "macro_7_consent"),
}

MACRO_NUMERIC_FIELDS = {
    "age", "nationality_2", "nationality_3", "nationality_4",
    "ssalary", "esalary", "lastsalary_2", "expectedsalary",
    "ref1phn", "ref1phn_2",
}


def _normalise_nric(value):
    """Compare NRIC/ID values consistently (ignore spaces/hyphens/case)."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _macro_section_validation(app, section, section_data, request=None, data_context=None):
    """Return validation errors for a single Macro Kiosk section."""
    errors = []
    required = MACRO_REQUIRED_FIELDS.get(section, ())

    for field_name in required:
        value = section_data.get(field_name, "")
        if isinstance(value, (list, tuple)):
            value = next((str(v).strip() for v in value if str(v).strip()), "")
        if not str(value or "").strip():
            errors.append(f"{field_name.replace('_', ' ').title()} is required.")

    if section == "macro_section_1":
        # Passport photo is required only until one has already been saved for
        # this application. This is what makes refresh/re-edit work correctly.
        has_photo = app.files.filter(field_name="macro_doc_1_1").exists()
        new_photo = bool(request and request.FILES.get("macro_doc_1_1"))
        if not has_photo and not new_photo:
            errors.append("Passport size photo is required.")

        occupations = {
            key for key in (
                "macro_1_checkbox_1", "macro_1_checkbox_2", "macro_1_checkbox_3",
                "macro_1_checkbox_4", "macro_1_checkbox_5", "macro_1_checkbox_6",
                "macro_1_checkbox_7", "macro_1_checkbox_8",
            ) if section_data.get(key)
        }
        if len(occupations) != 1:
            errors.append("Please select exactly one position applied for.")

        age = str(section_data.get("age", "")).strip()
        if age and (not age.isdigit() or not 18 <= int(age) <= 100):
            errors.append("Age must be a whole number between 18 and 100.")

        for field_name in ("nationality_2", "nationality_3", "nationality_4"):
            value = str(section_data.get(field_name, "")).strip()
            if value and not value.isdigit():
                label = {"nationality_2": "House Phone No.", "nationality_3": "Office Phone No.", "nationality_4": "Handphone No."}[field_name]
                errors.append(f"{label} must contain numbers only.")

        email = str(section_data.get("email", "")).strip()
        if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            errors.append("Please enter a valid email address.")

        # One candidate may apply to multiple companies/applications with the
        # same NRIC. A different candidate may not reuse that NRIC.
        nric = _normalise_nric(section_data.get("passportnumber"))
        if nric:
            for other in JobApplication.objects.exclude(pk=app.pk).exclude(candidate_id=app.candidate_id).only("id", "candidate_id", "data"):
                other_data = other.data if isinstance(other.data, dict) else {}
                other_macro = other_data.get("macro_section_1", {})
                if isinstance(other_macro, dict) and _normalise_nric(other_macro.get("passportnumber")) == nric:
                    errors.append("This NRIC/ID number is already registered to another candidate.")
                    break

    elif section == "macro_section_2":
        for field_name in ("ssalary", "esalary"):
            value = str(section_data.get(field_name, "")).strip()
            if value and not value.isdigit():
                errors.append("Salary fields must contain numbers only.")
        start = str(section_data.get("datefrom", "")).strip()
        end = str(section_data.get("dateto", "")).strip()
        if start and end and start > end:
            errors.append("Date To must be on or after Date From.")

    elif section == "macro_section_3":
        start = str(section_data.get("yearfrom", "")).strip()
        end = str(section_data.get("yearto", "")).strip()
        if start and end and start > end:
            errors.append("End date must be on or after start date.")

    elif section == "macro_section_4":
        for field_name in ("lastsalary_2", "expectedsalary", "ref1phn", "ref1phn_2"):
            value = str(section_data.get(field_name, "")).strip()
            if value and not value.isdigit():
                errors.append(f"{field_name.replace('_', ' ').title()} must contain numbers only.")
        for field_name in ("ref1email", "ref1email_2"):
            value = str(section_data.get(field_name, "")).strip()
            if value and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                errors.append("Please enter a valid reference email address.")

    elif section == "macro_section_5":
        has_signature = app.files.filter(field_name="macro_doc_5_1").exists()
        new_signature = bool(request and request.FILES.get("macro_doc_5_1"))
        if not has_signature and not new_signature:
            errors.append("Candidate signature file is required.")

        section_nric = _normalise_nric(section_data.get("macro_5_nric"))
        declared = _normalise_nric(section_data.get("nric"))
        source_data = data_context if isinstance(data_context, dict) else (app.data if isinstance(app.data, dict) else {})
        personal = source_data.get("macro_section_1", {})
        personal_nric = _normalise_nric(personal.get("passportnumber") if isinstance(personal, dict) else "")

        # Personal Information is the single source of truth for the candidate ID.
        # Background Check and HR PDPA must contain exactly the same normalized NRIC/ID.
        if personal_nric:
            if section_nric != personal_nric:
                errors.append("NRIC/Passport No. must match the NRIC/ID used in Personal Information.")
            if declared != personal_nric:
                errors.append("NRIC/Passport No. must match the NRIC/ID used in Personal Information.")

    elif section == "macro_section_7":
        section_nric = _normalise_nric(section_data.get("macro_7_nric"))
        source_data = data_context if isinstance(data_context, dict) else (app.data if isinstance(app.data, dict) else {})
        personal = source_data.get("macro_section_1", {})
        personal_nric = _normalise_nric(personal.get("passportnumber") if isinstance(personal, dict) else "")
        if personal_nric and section_nric != personal_nric:
            errors.append("NRIC/ID No. must match the NRIC/ID used in Personal Information.")

    return list(dict.fromkeys(errors))


def _validate_final_application(app, data, request=None):
    errors = []
    if is_macro_kiosk_application(app):
        for macro_section in (
            "macro_section_1", "macro_section_2", "macro_section_3",
            "macro_section_4", "macro_section_5", "macro_section_6",
            "macro_section_7",
        ):
            section_data = data.get(macro_section, {}) if isinstance(data.get(macro_section), dict) else {}
            errors.extend(_macro_section_validation(app, macro_section, section_data, request=request, data_context=data))
        return list(dict.fromkeys(errors))
    personal = data.get("personal_information", {}) if isinstance(data.get("personal_information"), dict) else {}
    consent = data.get("consent", {}) if isinstance(data.get("consent"), dict) else {}
    declaration = data.get("declaration", {}) if isinstance(data.get("declaration"), dict) else {}
    reference = data.get("reference", {}) if isinstance(data.get("reference"), dict) else {}
    expectations = data.get("employment_expectations", {}) if isinstance(data.get("employment_expectations"), dict) else {}
    if not (personal.get("name") or personal.get("full_name")):
        errors.append("Personal Information: Name is required.")
    if not personal.get("email"):
        errors.append("Personal Information: Email is required.")
    if is_insitemy_application(app):
        if not consent.get("consentname") and not consent.get("appname"):
            errors.append("Consent Form: applicant name is required.")
        if not declaration.get("declaration_appname") or not declaration.get("dateapplied"):
            errors.append("Applicant Declaration: applicant name and date are required.")
        if not reference.get("ref1name"):
            errors.append("Reference Details: at least one reference is required.")
        expectation_values = [str(value).strip().lower() for value in expectations.values() if value is not None]
        if not expectation_values or all(value in {"", "please select"} for value in expectation_values):
            errors.append("Employment Expectations: please complete the section.")
        if not app.files.exists():
            errors.append("Document Submission: at least one document is required.")
    return errors

@login_required(login_url="/login/")
def candidate_application(request, token):
    invitation = get_object_or_404(
        Invitation.objects.select_related(
            "application__candidate",
            "application__company",
        ),
        token=token,
    )

    app = invitation.application

    # ============================================================
    # CANDIDATE ACCESS CHECK
    # ============================================================

    if get_role(request.user) != "candidate":
        messages.error(
            request,
            "Only a candidate account can complete this application.",
        )
        return redirect("dashboard")

    candidate = candidate_for_user(request.user)

    if not candidate or candidate.pk != app.candidate_id:
        messages.error(
            request,
            "This invitation is not assigned to your candidate account.",
        )
        return redirect("dashboard")

    # ============================================================
    # ALREADY SUBMITTED
    # ============================================================

    if app.status == "submitted":
        return render(
            request,
            "core/submitted-application.html",
            {
                "candidate_submission": True,
                "application": app,
            },
        )

    # ============================================================
    # MARK INVITATION AS OPENED
    # ============================================================

    if not invitation.opened_at:
        invitation.opened_at = timezone.now()
        invitation.save(
            update_fields=["opened_at"]
        )
        # Record the first opening separately so HR can see the full
        # invitation -> opened -> filled -> submitted flow.
        if not app.audit_trails.filter(action="invite_viewed").exists():
            _audit_event(request, app, "invite_viewed")

    # ============================================================
    # SELECT APPLICATION TEMPLATE
    #
    # Company is a ForeignKey.
    # DO NOT use:
    # JobApplication.INSITEMY
    # JobApplication.MACRO
    # ============================================================

    onboarding = HROnboardingForm.objects.filter(
        hr_user__profile__company_name__iexact=app.company.name,
        status="ready",
    ).first()
    if onboarding and onboarding.connected_form_slug == "nevigate":
        if request.method == "POST":
            submitted = {
                key: value for key, value in request.POST.items()
                if key not in {"csrfmiddlewaretoken"}
            }
            app.data = {"nevigate_application": submitted}
            app.status = "submitted"
            app.submitted_at = timezone.now()
            app.save(update_fields=["data", "status", "submitted_at", "updated_at"])
            invitation.completed_at = timezone.now()
            invitation.save(update_fields=["completed_at"])
            _audit_event(request, app, "application_submitted")
            subject = f"{app.company.name} employment application submitted"
            body = (
                f"A candidate has submitted the Nevigate Global Network employment application "
                f"for {app.company.name}.\n\nApplication ID: {app.pk}\n"
                f"Please sign in to eDocs to review the submitted application."
            )
            try:
                hr_users = User.objects.filter(
                    profile__company_name__iexact=app.company.name,
                    is_active=True,
                ).exclude(email="")
                recipients = list(hr_users.values_list("email", flat=True).distinct())
                if recipients:
                    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
                    for recipient in recipients:
                        EmailLog.objects.create(recipient=recipient, subject=subject, success=True)
            except Exception as exc:
                EmailLog.objects.create(
                    recipient=(recipients[0] if "recipients" in locals() and recipients else settings.DEFAULT_FROM_EMAIL),
                    subject=subject, success=False, error=str(exc)
                )
            _notify_hr_company(app.company, "Employment application submitted", body, app, reverse("received_applications"))
            messages.success(request, "Your Nevigate employment application has been submitted. The company has been notified by email.")
            return redirect("submitted_applications")
        return render(request, "core/Nevigate.html", {"application": app})

    if onboarding and onboarding.connected_form_slug == "raddish" or "raddish" in app.company.name.lower():
        if request.method == "POST":
            submitted = {key: value for key, value in request.POST.items() if key not in {"csrfmiddlewaretoken", "final_submit"}}
            required_values = [submitted.get("full_name", "").strip(), submitted.get("email", "").strip(), submitted.get("position_applied", "").strip(), submitted.get("declaration_confirmed", "").strip()]
            if not all(required_values) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", submitted.get("email", "").strip()):
                messages.error(request, "Please provide your name, a valid email, position applied for, and accept the declaration.")
                return render(request, "core/raddish-application.html", {"application": app, "saved_data": submitted,
                    "personal_fields": [("position_applied", "Application for employment as"), ("full_name", "Full name"), ("email", "Email"), ("home_address", "Home address"), ("correspondence_address", "Correspondence address"), ("home_telephone", "Home telephone"), ("correspondence_telephone", "Correspondence telephone"), ("date_of_birth", "Date of birth"), ("age", "Age"), ("marital_status", "Marital status"), ("nationality", "Nationality"), ("religion", "Religion"), ("nric_no", "NRIC No."), ("passport_no", "Passport No."), ("epf_no", "EPF No."), ("socso_no", "SOCSO No.")],
                    "family_fields": [("spouse_details", "Spouse — name, age, occupation"), ("father_details", "Father — name, age, occupation"), ("mother_details", "Mother — name, age, occupation"), ("children_details", "Children — names, ages, occupation")], "education_fields": [("school_institution", "School / institution / college / university"), ("education_dates", "Dates joined and graduated"), ("highest_qualification", "Highest standard / qualification / grade")], "other_fields": [("serious_offence", "Ever convicted of a serious offence? If yes, give details"), ("dismissed_from_employment", "Ever dismissed from employment? If yes, give reasons"), ("serious_illness", "Any serious illness? If yes, indicate nature"), ("available_start_date", "If successful, when can you start?"), ("expected_salary", "Expected salary")]})
            app.data = {"raddish_application": submitted}
            app.status = "submitted"
            app.submitted_at = timezone.now()
            app.save(update_fields=["data", "status", "submitted_at", "updated_at"])
            invitation.completed_at = timezone.now()
            invitation.save(update_fields=["completed_at"])
            _audit_event(request, app, "application_submitted")
            _notify_hr_company(app.company, "Employment application submitted", f"A candidate submitted the {app.company.name} employment form.", app, reverse("received_applications"))
            messages.success(request, "Your employment application has been submitted successfully.")
            return render(request, "core/submitted-application.html", {"candidate_submission": True, "application": app})
        return render(request, "core/raddish-application.html", {
            "application": app,
            "saved_data": (app.data or {}).get("raddish_application", {}) if isinstance(app.data, dict) else {},
            "personal_fields": [("position_applied", "Application for employment as"), ("full_name", "Full name"), ("email", "Email"), ("home_address", "Home address"), ("correspondence_address", "Correspondence address"), ("home_telephone", "Home telephone"), ("correspondence_telephone", "Correspondence telephone"), ("date_of_birth", "Date of birth"), ("age", "Age"), ("marital_status", "Marital status"), ("nationality", "Nationality"), ("religion", "Religion"), ("nric_no", "NRIC No."), ("passport_no", "Passport No."), ("epf_no", "EPF No."), ("socso_no", "SOCSO No.")],
            "family_fields": [("spouse_details", "Spouse — name, age, occupation"), ("father_details", "Father — name, age, occupation"), ("mother_details", "Mother — name, age, occupation"), ("children_details", "Children — names, ages, occupation")],
            "education_fields": [("school_institution", "School / institution / college / university"), ("education_dates", "Dates joined and graduated"), ("highest_qualification", "Highest standard / qualification / grade")],
            "other_fields": [("serious_offence", "Ever convicted of a serious offence? If yes, give details"), ("dismissed_from_employment", "Ever dismissed from employment? If yes, give reasons"), ("serious_illness", "Any serious illness? If yes, indicate nature"), ("available_start_date", "If successful, when can you start?"), ("expected_salary", "Expected salary")],
        })

    if is_insitemy_application(app):
        template = "core/insitemy.html"
    elif is_macro_kiosk_application(app):
        template = "core/macro-kiosk.html"
    else:
        raise Http404("Application company template not found.")

    saved_data = _candidate_defaults(app)

    # ============================================================
    # POST
    # ============================================================

    if request.method == "POST":

        # --------------------------------------------------------
        # CURRENT SECTION
        # --------------------------------------------------------

        section = _section_from_request(request)

        # --------------------------------------------------------
        # FINAL SUBMISSION FLAG
        # --------------------------------------------------------

        is_final = (
            request.POST.get(
                "final_submit",
                "",
            ).lower()
            in {"true", "1", "yes", "on"}
        )

        # --------------------------------------------------------
        # COLLECT POST DATA
        # --------------------------------------------------------

        all_post_data = _collect_post_data(request)

        allowed_names = (
            _section_field_names(
                section,
                request,
            )
            if section
            else set(all_post_data)
        )

        section_data = {
            key: value
            for key, value in all_post_data.items()
            if key in allowed_names
        }

        # ========================================================
        # INSITEMY FINAL SUBMISSION
        #
        # Insitemy has one visual form containing multiple
        # logical sections.
        # ========================================================

        if (
            is_final
            and is_insitemy_application(app)
        ):

            for final_section in (
                "personal_information",
                "iq_test",
                "consent",
                "documents",
                "employment_expectations",
                "reference",
                "declaration",
            ):

                names = _section_field_names(
                    final_section,
                    request,
                )

                section_values = {
                    key: value
                    for key, value in all_post_data.items()
                    if key in names
                }

                if section_values:

                    saved_data = _merge_section(
                        saved_data,
                        final_section,
                        section_values,
                    )

            # Final validation is based on declaration
            section = "declaration"

            section_data = {
                key: value
                for key, value in all_post_data.items()
                if key in _section_field_names(
                    "declaration",
                    request,
                )
            }

        if is_final and is_macro_kiosk_application(app):
            for final_section in (
                "macro_section_1","macro_section_2","macro_section_3",
                "macro_section_4","macro_section_5","macro_section_6","macro_section_7",
            ):
                names = _section_field_names(final_section, request)
                values = {k: value for k, value in all_post_data.items() if k in names}
                if values:
                    saved_data = _merge_section(saved_data, final_section, values)
            section = "macro_section_7"
            section_data = {
                k: value for k, value in all_post_data.items()
                if k in _section_field_names(section, request)
            }

        # ========================================================
        # FILE VALIDATION
        # ========================================================

        for uploaded in request.FILES.values():

            if uploaded.size > MAX_UPLOAD_SIZE:

                messages.error(
                    request,
                    (
                        f"{uploaded.name} is larger than 1 MB. "
                        "Please choose a smaller file."
                    ),
                )

                target = reverse(
                    "candidate_application",
                    kwargs={
                        "token": invitation.token,
                    },
                )

                return redirect(
                    f"{target}?section={section}"
                )

            if (
                Path(
                    uploaded.name
                ).suffix.lower()
                not in ALLOWED_UPLOAD_EXTENSIONS
            ):

                messages.error(
                    request,
                    (
                        f"{uploaded.name} is not an "
                        "allowed file type."
                    ),
                )

                target = reverse(
                    "candidate_application",
                    kwargs={
                        "token": invitation.token,
                    },
                )

                return redirect(
                    f"{target}?section={section}"
                )

        # ========================================================
        # PERSONAL INFORMATION
        # ========================================================

        if section == "personal_information":

            same = (
                request.POST.get(
                    "same_as_residential",
                    "",
                )
                in {"on", "true", "1", "yes"}
            )

            section_data[
                "same_as_residential"
            ] = same

            if same:

                for suffix in (
                    "address",
                    "city",
                    "state",
                    "country",
                    "pincode",
                ):

                    value = request.POST.get(
                        f"residential_{suffix}",
                        "",
                    )

                    if value:

                        section_data[
                            f"permanent_{suffix}"
                        ] = value

        # ========================================================
        # SERVER-SIDE SECTION VALIDATION
        # ========================================================
        if is_macro_kiosk_application(app):
            section_errors = _macro_section_validation(
                app, section, section_data, request=request
            )
            if section_errors:
                for error in section_errors:
                    messages.error(request, error)
                target = reverse("candidate_application", kwargs={"token": invitation.token})
                return redirect(f"{target}?section={section}")

        # ========================================================
        # SAVE APPLICATION
        # ========================================================

        with transaction.atomic():

            had_filled_event = app.audit_trails.filter(
                action="application_filled"
            ).exists()

            saved_data = _merge_section(
                saved_data,
                section,
                section_data,
            )

            if section_data and not had_filled_event:
                _audit_event(request, app, "application_filled")

            # ----------------------------------------------------
            # SAVE UPLOADED FILES
            # ----------------------------------------------------

            uploaded_records = []

            for (
                field_name,
                uploaded,
            ) in request.FILES.items():

                if uploaded:

                    saved_file = _save_uploaded_file(
                        app,
                        field_name,
                        uploaded,
                    )

                    uploaded_records.append(
                        {
                            "id": saved_file.id,
                            "field_name": field_name,
                            "filename": saved_file.file.name,
                        }
                    )

            # ----------------------------------------------------
            # ADD UPLOADED FILES TO SAVED DATA
            # ----------------------------------------------------

            if uploaded_records:

                existing_documents = saved_data.get(
                    "documents",
                    [],
                )

                if not isinstance(
                    existing_documents,
                    list,
                ):

                    existing_documents = []

                existing_documents.extend(
                    uploaded_records
                )

                saved_data[
                    "documents"
                ] = existing_documents

            app.data = saved_data

            # ====================================================
            # UPDATE CANDIDATE INFORMATION
            # ====================================================

            if section == "personal_information":

                name = (
                    section_data.get("name")
                    or section_data.get("full_name")
                )

                phone = section_data.get(
                    "phone"
                )

                email = section_data.get(
                    "email"
                )

                if name:

                    candidate.full_name = (
                        str(name).strip()
                    )

                if phone:

                    candidate.phone = (
                        str(phone).strip()
                    )

                if email:

                    new_email = str(email).strip().lower()

                    if User.objects.exclude(
                        pk=request.user.pk
                    ).filter(email__iexact=new_email).exists():
                        messages.error(
                            request,
                            "That email address is already in use.",
                        )
                    else:
                        candidate.email = new_email
                        request.user.email = new_email
                        request.user.username = new_email
                        request.user.save(
                            update_fields=["email", "username"]
                        )

                candidate.save()

            # ====================================================
            # FINAL SUBMISSION
            # ====================================================

            if is_final:

                errors = _validate_final_application(
                    app,
                    saved_data,
                    request=request,
                )

                if errors:

                    messages.error(
                        request,
                        (
                            "Please complete the required "
                            "sections before submitting."
                        ),
                    )

                    for error in errors:

                        messages.error(
                            request,
                            error,
                        )

                    app.save(
                        update_fields=[
                            "data",
                            "updated_at",
                        ]
                    )

                    return redirect(
                        "candidate_application",
                        token=invitation.token,
                    )

                # ------------------------------------------------
                # MARK APPLICATION AS SUBMITTED
                # ------------------------------------------------

                app.status = "submitted"

                app.submitted_at = timezone.now()

                app.save(
                    update_fields=[
                        "data",
                        "status",
                        "submitted_at",
                        "updated_at",
                    ]
                )
                _audit_event(request, app, "submitted")

                # ------------------------------------------------
                # COMPLETE INVITATION
                # ------------------------------------------------

                invitation.completed_at = timezone.now()

                invitation.active = False

                invitation.save(
                    update_fields=[
                        "completed_at",
                        "active",
                    ]
                )

                # ------------------------------------------------
                # CREATE ROLE-SPECIFIC NOTIFICATIONS
                # ------------------------------------------------

                applicant_name = normalize_person_name(
                    candidate.full_name or candidate.email
                ) or "Candidate"
                application_message = (
                    f"{app.company.name} application from {applicant_name} "
                    "has been completed."
                )
                _notify_hr_company(
                    app.company, "New job application received", application_message,
                    app, reverse("received_applications") + f"#application-row-{app.id}"
                )
                _notify_role(
                    "administration", "New job application received", application_message,
                    app, reverse("received_applications") + f"#application-row-{app.id}"
                )
                _notify_candidate_agents(
                    candidate, "Candidate application submitted", application_message,
                    app, reverse("submitted_applications") + f"#application-row-{app.id}"
                )
                _notify_candidate(
                    candidate, "Application submitted successfully",
                    f"Your {app.company.name} application has been submitted successfully.",
                    app, reverse("submitted_applications") + f"#application-row-{app.id}"
                )

                # ------------------------------------------------
                # SEND HR EMAIL
                # ------------------------------------------------

                hr_users = _hr_users_for_company(app.company)
                hr_emails = [u.email.strip() for u in hr_users if (u.email or "").strip()]

                # Backward-compatible fallback for installations that still use
                # a single HR_NOTIFICATION_EMAIL and have no company HR account.
                if not hr_emails:
                    fallback_hr_email = getattr(settings, "HR_NOTIFICATION_EMAIL", "")
                    if fallback_hr_email:
                        hr_emails = [fallback_hr_email.strip()]

                if hr_emails:
                    try:
                        send_mail(
                            f"New {app.company.name} application received",
                            (
                                f"Candidate: {candidate.full_name or 'Candidate'}\n"
                                f"Email: {candidate.email}\n"
                                f"Application ID: {app.id}\n"
                                f"Status: Submitted\n"
                                f"Open Received Applications: {request.build_absolute_uri(reverse('received_applications'))}"
                            ),
                            settings.DEFAULT_FROM_EMAIL,
                            list(dict.fromkeys(hr_emails)),
                            fail_silently=True,
                        )
                    except Exception:
                        pass

                # ------------------------------------------------
                # SHOW SUBMITTED PAGE
                # ------------------------------------------------

                return render(
                    request,
                    "core/submitted-application.html",
                    {
                        "candidate_submission": True,
                        "application": app,
                    },
                )

            # ====================================================
            # NORMAL SAVE = DRAFT
            # ====================================================

            app.status = "draft"

            app.save(
                update_fields=[
                    "data",
                    "status",
                    "updated_at",
                ]
            )

        # ========================================================
        # SUCCESS
        # ========================================================

        messages.success(
            request,
            "Your information has been saved successfully.",
        )

        target = reverse(
            "candidate_application",
            kwargs={
                "token": invitation.token,
            },
        )

        return redirect(
            f"{target}?section={section}"
        )

    # ============================================================
    # GET
    # ============================================================

    return render(
        request,
        template,
        {
            "candidate_application": True,
            "application": app,
            "saved_data": saved_data,
            "uploaded_files": (
                app.files
                .all()
                .order_by("-uploaded_at")
            ),
            "macro_photo": app.files.filter(field_name="macro_doc_1_1").order_by("-uploaded_at").first(),
            "macro_signature": app.files.filter(field_name="macro_doc_5_1").order_by("-uploaded_at").first(),
            "application_id": app.pk,
            "macro_saved_files": [
                {"field": f.field_name, "url": f.file.url, "name": f.file.name}
                for f in app.files.all().order_by("-uploaded_at")
            ],
        },
    )
def _safe_invitation(app):
    try:
        return app.invitation
    except Invitation.DoesNotExist:
        return None


def _latest_audit_event(events, actions, user=None, fallback=False):
    """Return the newest matching event. Events are ordered newest first."""
    fallback_event = None
    for event in events:
        if event.action not in actions:
            continue
        if fallback_event is None:
            fallback_event = event
        if user is not None and event.user_id != user.id:
            continue
        return event
    if user is not None and fallback:
        return fallback_event
    if user is None:
        return fallback_event
    return None


def _access_request_for(app, role_name, user=None, fallback=False):
    requests = list(app.access_requests.all())
    if user is not None:
        for access_request in requests:
            if access_request.requested_by_id == user.id:
                return access_request
        if not fallback:
            return None
    for access_request in requests:
        if get_role(access_request.requested_by) == role_name:
            return access_request
    return None


def _invite_received_at(app, events):
    invitation = _safe_invitation(app)
    if invitation and (invitation.opened_at or invitation.sent_at):
        return invitation.opened_at or invitation.sent_at
    opened = _latest_audit_event(events, {"invite_viewed"})
    sent = _latest_audit_event(events, {"invite_sent"})
    event = opened or sent
    return event.created_at if event else None


def _audit_step(title, icon, color, date=None, description="", details=None):
    return {
        "title": title,
        "icon": icon,
        "color": color,
        "date": date,
        "description": description,
        "details": details,
    }


def _view_detail_boxes(view_event, download_event, location_label, ip_label, ip_from_view_only=False):
    if ip_from_view_only:
        source = view_event
    elif view_event and (view_event.location or view_event.ip_address):
        source = view_event
    else:
        source = download_event or view_event
    location = source.location if source and source.location else ""
    ip_address = str(source.ip_address) if source and source.ip_address else ""
    return [
        {"label": "Viewed", "date": view_event.created_at if view_event else None, "text": ""},
        {"label": "Downloaded", "date": download_event.created_at if download_event else None, "text": ""},
        {"label": location_label, "date": None, "text": location},
        {"label": ip_label, "date": None, "text": ip_address},
    ]


def _forward_recipient_email(app, event):
    if not event:
        return ""
    prefix = f"Application #{app.id} was forwarded to "
    best_email = ""
    best_gap = None
    for note in app.notification_set.all():
        message = (note.message or "").strip()
        if not message.startswith(prefix):
            continue
        email = message[len(prefix):].strip().rstrip(".")
        gap = abs((note.created_at - event.created_at).total_seconds())
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best_email = email
    return best_email


def _forward_and_hr_access_steps(app, events, user=None, scope_forward=False):
    forward_event = _latest_audit_event(
        events,
        {"forwarded"},
        user if scope_forward else None,
    )
    hr_view = _latest_audit_event(events, {"hr_viewed", "view"})
    hr_download = _latest_audit_event(events, {"hr_downloaded", "download"})
    recipient = _forward_recipient_email(app, forward_event)
    if recipient:
        forward_description = f"The agent forwarded this application to {recipient}."
    else:
        forward_description = "The agent forwarded this application to HR."
    return [
        _audit_step(
            "Forwarding to HR by Agent",
            "bi-share",
            "blue",
            forward_event.created_at if forward_event else None,
            forward_description,
        ),
        _audit_step(
            "Application viewed / downloaded by HR Access",
            "bi-eye",
            "dark",
            details=_view_detail_boxes(
                hr_view,
                hr_download,
                "HR Location",
                "HR IP Address",
            ),
        ),
    ]


def _hr_audit_steps(app, events, user, scoped):
    access_request = _access_request_for(app, "hr", user if scoped else None, fallback=True)
    view_event = _latest_audit_event(
        events, {"hr_viewed", "view"}, user if scoped else None, fallback=True
    )
    download_event = _latest_audit_event(
        events, {"hr_downloaded", "download"}, user if scoped else None, fallback=True
    )
    return [
        _audit_step(
            "Invite Received",
            "bi-envelope-check",
            "blue",
            _invite_received_at(app, events),
            "The invitation was received and the application process was opened.",
        ),
        _audit_step(
            "Application Submitted to HR",
            "bi-file-earmark-check",
            "purple",
            app.submitted_at,
            "Candidate completed and submitted the application to HR.",
        ),
        _audit_step(
            "Access Request Received",
            "bi-person-lock",
            "orange",
            access_request.requested_at if access_request else None,
            "HR requested permission to access the candidate application.",
        ),
        _audit_step(
            "Access Granted",
            "bi-shield-check",
            "green",
            access_request.approved_at if access_request else None,
            "Candidate approved HR access to the application.",
        ),
        _audit_step(
            "Application Viewed / Download",
            "bi-eye",
            "dark",
            details=_view_detail_boxes(
                view_event,
                download_event,
                "HR Location",
                "HR IP Address",
            ),
        ),
    ]


def _agent_audit_steps(app, events, user, scoped):
    access_request = _access_request_for(app, "agent", user if scoped else None, fallback=False)
    view_event = _latest_audit_event(events, {"agent_viewed"}, user if scoped else None)
    download_event = _latest_audit_event(events, {"agent_downloaded"}, user if scoped else None)
    steps = [
        _audit_step(
            "Invite Received",
            "bi-envelope-check",
            "blue",
            _invite_received_at(app, events),
            "The invitation was received and the application process was opened.",
        ),
        _audit_step(
            "Application Submitted to Agent",
            "bi-file-earmark-check",
            "purple",
            app.submitted_at,
            "Candidate completed and submitted the application to the agent.",
        ),
        _audit_step(
            "Request Received",
            "bi-person-lock",
            "orange",
            access_request.requested_at if access_request else None,
            "The agent requested permission to access the candidate application.",
        ),
        _audit_step(
            "Access Granted",
            "bi-shield-check",
            "green",
            access_request.approved_at if access_request else None,
            "Candidate approved agent access to the application.",
        ),
        _audit_step(
            "Application Viewed / Download by Agent",
            "bi-eye",
            "dark",
            details=_view_detail_boxes(
                view_event,
                download_event,
                "Agent Location",
                "Agent IP Address",
            ),
        ),
    ]
    steps.extend(_forward_and_hr_access_steps(app, events, user, scope_forward=scoped))
    return steps


def _candidate_audit_steps(app, events):
    access_requests = list(app.access_requests.all())
    latest_request = access_requests[0] if access_requests else None
    view_event = _latest_audit_event(events, {"view", "hr_viewed"})
    download_event = _latest_audit_event(events, {"download", "hr_downloaded"})
    steps = [
        _audit_step(
            "HR Invite Received / Opened",
            "bi-envelope-check",
            "blue",
            app.created_at,
            "HR invitation was received and the application process was opened.",
        ),
        _audit_step(
            "Application Submitted",
            "bi-file-earmark-check",
            "purple",
            app.submitted_at,
            "Candidate completed and submitted the application.",
        ),
        _audit_step(
            "Access Request Received",
            "bi-person-lock",
            "orange",
            latest_request.requested_at if latest_request else None,
            "HR requested permission to access the candidate application.",
        ),
        _audit_step(
            "Candidate Approved",
            "bi-shield-check",
            "green",
            latest_request.approved_at if latest_request else None,
            "Candidate approved HR access to the application.",
        ),
        # _audit_step(
        #     "Application Viewed / Download",
        #     "bi-eye",
        #     "dark",
        #     details=_view_detail_boxes(
        #         view_event,
        #         download_event,
        #         "HR Location",
        #         "HR IP Address",
        #         ip_from_view_only=True,
        #     ),
        # ),
    ]
    steps.extend(_forward_and_hr_access_steps(app, events))
    return steps


def _audit_modal_steps(app, role, user):
    events = list(app.audit_trails.all())
    if role == "candidate":
        return _candidate_audit_steps(app, events)
    if role == "agent":
        return _agent_audit_steps(app, events, user, scoped=True)
    if role == "hr":
        return _hr_audit_steps(app, events, user, scoped=True)
    invitation = _safe_invitation(app)
    inviter_role = get_role(invitation.invited_by) if invitation and invitation.invited_by_id else None
    if inviter_role == "agent":
        return _agent_audit_steps(app, events, user, scoped=False)
    steps = _hr_audit_steps(app, events, user, scoped=False)
    if any(event.action == "forwarded" for event in events):
        steps.extend(_forward_and_hr_access_steps(app, events))
    return steps


def _prepare_application_audit(app, role, user):
    events = list(app.audit_trails.all())
    app.hr_viewed = any(event.action in {"view", "hr_viewed"} for event in events)
    access_requests = list(app.access_requests.all())
    app.latest_access_request = access_requests[0] if access_requests else None
    app.audit_events = events
    app.latest_view_event = _latest_audit_event(events, {"view", "hr_viewed"})
    app.latest_download_event = _latest_audit_event(events, {"download", "hr_downloaded"})
    if role == "agent":
        app.agent_access_request = _access_request_for(app, "agent", user, fallback=False)
    app.audit_steps = _audit_modal_steps(app, role, user)


@login_required(login_url="/login/")
def submitted_applications(request):

    role = get_role(request.user)

    apps = (
        JobApplication.objects
        .filter(
            status__in={
                "submitted",
                "reviewed",
                "shortlisted",
                "rejected",
            }
        )
        .select_related(
            "candidate",
            "company",
            "invitation",
            "invitation__invited_by",
        )
        .prefetch_related(
            "access_requests__requested_by",
            "audit_trails__user",
            "notification_set",
        )
        .order_by("-submitted_at")
    )

    if role == "candidate":

        candidate = candidate_for_user(request.user)

        if candidate:
            apps = apps.filter(candidate=candidate)
        else:
            apps = apps.none()

    elif role == "agent":
        # Submitted Applications for an agent means applications that this
        # agent has explicitly forwarded to HR.
        apps = apps.filter(
            audit_trails__user=request.user,
            audit_trails__action="forwarded",
            archived=False,
        ).distinct()

    elif role == "hr":
        company_name = hr_company_name(request.user)
        apps = apps.filter(company__name__iexact=company_name)

    elif role != "administration":
        return redirect("dashboard")

    # ---------------------------------------------------------
    # CLEAN APPLICANT NAME FOR DISPLAY
    # ---------------------------------------------------------

    for app in apps:

        if not app.candidate:
            app.display_name = "Candidate"
            _prepare_application_audit(app, role, request.user)
            continue

        display_name = ""

        # First preference:
        # saved application personal information
        if isinstance(app.data, dict):

            personal = app.data.get(
                "personal_information",
                {}
            )

            if isinstance(personal, dict):

                display_name = (
                    personal.get("name")
                    or personal.get("full_name")
                    or ""
                )

        # Fallback to Candidate model
        if not display_name:
            display_name = app.candidate.full_name

        # Clean duplicate / legacy values
        display_name = normalize_person_name(display_name)

        if not display_name:
            display_name = "Candidate"

        app.display_name = display_name

        _prepare_application_audit(app, role, request.user)

    return render(
        request,
        "core/submitted-application.html",
        {
            "applications": apps,
            "role": role,
        },
    )

@role_required("hr", "agent", "administration")
def received_applications(request):
    role = get_role(request.user)

    apps = (
        JobApplication.objects
        .filter(
            status__in={"submitted", "reviewed", "shortlisted", "rejected"},
            archived=False,
        )
        .select_related("candidate", "company", "invitation", "invitation__invited_by")
        .prefetch_related(
            "files",
            "access_requests__requested_by",
            "audit_trails__user",
            "notification_set",
        )
        .order_by("-submitted_at")
    )

    if role == "hr":
        company_name = hr_company_name(request.user)
        apps = apps.filter(company__name__iexact=company_name)
    elif role == "agent":
        # Only show applications that this agent actually invited the candidate
        # to complete. Applications created by another route stay hidden.
        apps = apps.filter(invitation__invited_by=request.user)

    for app in apps:
        if app.candidate:
            app.candidate.full_name = normalize_person_name(app.candidate.full_name)

        app.hr_access_request = None
        if role == "hr":
            app.hr_access_request = (
                ApplicationAccessRequest.objects
                .filter(application=app, requested_by=request.user)
                .order_by("-requested_at")
                .first()
            )

        _prepare_application_audit(app, role, request.user)

    return render(
        request,
        "core/received-application.html",
        {
            "applications": apps,
            "role": role,
        },
    )


@role_required("hr", "agent", "administration")
def archive_application(request, pk):
    if request.method != "POST":
        return redirect("received_applications")

    app = get_object_or_404(
        JobApplication.objects.select_related("candidate", "company"),
        pk=pk,
        archived=False,
        status__in={"submitted", "reviewed", "shortlisted", "rejected"},
    )
    role = get_role(request.user)

    if role == "hr":
        expected = hr_company_name(request.user)
        if not app.company or app.company.name.strip().lower() != expected.lower():
            raise Http404
    elif role == "agent":
        if not AgentClient.objects.filter(agent=request.user, candidate=app.candidate).exists():
            raise Http404

    app.archived = True
    app.archived_at = timezone.now()
    app.save(update_fields=["archived", "archived_at", "updated_at"])

    _audit_event(request, app, "archived")
    messages.success(request, f"Application #{app.id} archived successfully.")
    return redirect("received_applications")


@role_required("hr", "agent")
def request_application_access(request, pk):
    """Send the candidate/client an approval request for a submitted application."""
    if request.method != "POST":
        return redirect("submitted_applications" if role == "agent" else "received_applications")

    app = get_object_or_404(
        JobApplication.objects.select_related("candidate", "company"),
        pk=pk,
        status__in={"submitted", "reviewed", "shortlisted", "rejected"},
    )

    role = get_role(request.user)
    if role == "hr":
        expected_company = hr_company_name(request.user)
        if not app.company or app.company.name.strip().lower() != expected_company.lower():
            messages.error(request, "You can request access only for applications belonging to your assigned company.")
            return redirect("submitted_applications" if role == "agent" else "received_applications")
    elif not AgentClient.objects.filter(agent=request.user, candidate=app.candidate).exists():
        raise Http404

    existing = (
        ApplicationAccessRequest.objects
        .filter(application=app, requested_by=request.user)
        .order_by("-requested_at")
        .first()
    )

    if existing and existing.status == "approved":
        messages.success(request, "Access has already been approved. You can view the application.")
        return redirect("submitted_applications" if role == "agent" else "received_applications")

    if existing and existing.status == "pending":
        messages.info(request, "Request Access sent to the candidate. Waiting for approval.")
        return redirect("submitted_applications" if role == "agent" else "received_applications")

    if not app.candidate or not app.candidate.email:
        messages.error(
            request,
            "The candidate does not have a valid email address for approval.",
        )
        return redirect("submitted_applications" if role == "agent" else "received_applications")

    access_request = ApplicationAccessRequest.objects.create(
        application=app,
        candidate=app.candidate,
        requested_by=request.user,
        status="pending",
    )

    request_message = (
        f"{request.user.get_full_name() or request.user.username} requested access "
        f"to Application #{app.id} ({app.company.name})."
    )

    approval_url = request.build_absolute_uri(
        reverse(
            "application_access_approval",
            kwargs={"token": access_request.token},
        )
    )
    deny_url = f"{approval_url}?decision=deny"

    candidate_name = normalize_person_name(app.candidate.full_name) or "Client"
    company_name = app.company.name if app.company else "Stalwart Business Solutions"

    email_html = render_to_string(
        "core/email-application-access-request.html",
        {
            "approval_url": approval_url,
            "deny_url": deny_url,
            "candidate_name": candidate_name,
            "company_name": company_name,
            "application_id": app.id,
        },
        request=request,
    )

    try:
        send_mail(
            subject="Approval required to share your job application",
            message=(
                f"Dear {candidate_name},\n\n"
                f"{request.user.get_full_name() or request.user.username} from "
                f"Stalwart Business Solutions has requested access to your "
                f"{company_name} job application (Application #{app.id}).\n\n"
                f"Please review and approve the request here:\n{approval_url}\n\n"
                f"If you want to deny the request, use this link:\n{deny_url}\n\n"
                "No application data is shared unless you approve."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[app.candidate.email],
            html_message=email_html,
            fail_silently=False,
        )
        access_request.email_sent_at = timezone.now()
        access_request.save(update_fields=["email_sent_at"])
    except Exception as exc:
        access_request.delete()
        EmailLog.objects.create(
            recipient=app.candidate.email,
            subject="Approval required to share your job application",
            success=False,
            error=str(exc),
        )
        messages.error(request, "The access request could not be emailed to the client. No request was created.")
        return redirect("submitted_applications" if role == "agent" else "received_applications")

    EmailLog.objects.create(
        recipient=app.candidate.email,
        subject="Approval required to share your job application",
        success=True,
    )
    _audit_event(request, app, "access_requested")

    _notify_candidate(
        app.candidate, "Application access requested", request_message,
        app, reverse("submitted_applications")
    )
    _notify_candidate_agents(
        app.candidate, "Application access requested", request_message,
        app, reverse("submitted_applications")
    )
    _notify_role(
        "administration", "Application access requested", request_message,
        app, reverse("received_applications")
    )
    messages.success(request, "Request Access sent to the candidate. An approval link has been emailed.")
    return redirect("submitted_applications" if role == "agent" else "received_applications")


def application_access_approval(request, token):
    """Let the candidate/client approve or deny an HR access request."""
    access_request = get_object_or_404(
        ApplicationAccessRequest.objects.select_related(
            "application__candidate", "application__company", "requested_by"
        ),
        token=token,
    )
    app = access_request.application
    candidate = access_request.candidate

    if access_request.status == "approved":
        return render(request, "core/application-access-approval.html", {
            "access_request": access_request, "application": app, "candidate": candidate,
            "already_approved": True,
        })
    if access_request.status == "denied":
        return render(request, "core/application-access-approval.html", {
            "access_request": access_request, "application": app, "candidate": candidate,
            "already_denied": True,
        })

    if request.method == "GET" and request.GET.get("decision", "").strip().lower() == "deny":
        action = "deny"
    elif request.method == "POST":
        action = request.POST.get("action", "").strip().lower()
    else:
        action = ""

    candidate_name = normalize_person_name(candidate.full_name) or candidate.email

    if action == "approve":
        access_request.status = "approved"
        access_request.approved_at = timezone.now()
        access_request.save(update_fields=["status", "approved_at"])
        _audit_event(request, app, "access_approved")

        approval_message = f"{candidate_name} approved your access request for Application #{app.id}."
        _notify_users([access_request.requested_by], "Application access approved", approval_message, app, reverse("received_applications"))
        _notify_role("administration", "Application access approved", approval_message, app, reverse("received_applications"))
        _notify_candidate(candidate, "Application access approved", "Your application access request has been approved.", app, reverse("submitted_applications"))
        _notify_candidate_agents(candidate, "Application access approved", approval_message, app, reverse("submitted_applications"))

        candidate_email = (candidate.email or "").strip()
        if candidate_email:
            try:
                send_mail(
                    subject=f"Application access approval confirmation - #{app.id}",
                    message=(f"Thank you, {candidate_name}. You approved access to Application #{app.id} for "
                             f"{access_request.requested_by.get_full_name() or access_request.requested_by.email}."),
                    from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[candidate_email], fail_silently=False,
                )
                EmailLog.objects.create(recipient=candidate_email, subject=f"Application access approval confirmation - #{app.id}", success=True)
            except Exception as exc:
                EmailLog.objects.create(recipient=candidate_email, subject=f"Application access approval confirmation - #{app.id}", success=False, error=str(exc))

        hr_email = (access_request.requested_by.email or "").strip()
        if hr_email:
            try:
                send_mail(
                    subject=f"Application access approved - #{app.id}",
                    message=(f"Access to Application #{app.id} has been approved by {candidate_name}.\n\n"
                             f"Open Received Applications: {request.build_absolute_uri(reverse('received_applications'))}"),
                    from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[hr_email], fail_silently=False,
                )
                EmailLog.objects.create(recipient=hr_email, subject=f"Application access approved - #{app.id}", success=True)
            except Exception as exc:
                EmailLog.objects.create(recipient=hr_email, subject=f"Application access approved - #{app.id}", success=False, error=str(exc))

        return render(request, "core/application-access-approval.html", {
            "access_request": access_request, "application": app, "candidate": candidate, "approved_now": True,
        })

    if action == "deny":
        access_request.status = "denied"
        access_request.denied_at = timezone.now()
        access_request.save(update_fields=["status", "denied_at"])

        denial_message = f"{candidate_name} denied your access request for Application #{app.id}."
        _notify_users([access_request.requested_by], "Application access denied", denial_message, app, reverse("received_applications"))
        _notify_role("administration", "Application access denied", denial_message, app, reverse("received_applications"))
        _notify_candidate(candidate, "Application access denied", "Your application access request was denied.", app, reverse("submitted_applications"))
        _notify_candidate_agents(candidate, "Application access denied", denial_message, app, reverse("submitted_applications"))

        hr_email = (access_request.requested_by.email or "").strip()
        if hr_email:
            try:
                send_mail(
                    subject=f"Application access denied - #{app.id}",
                    message=(f"Access to Application #{app.id} was denied by {candidate_name}.\n\n"
                             f"Open Received Applications: {request.build_absolute_uri(reverse('received_applications'))}"),
                    from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[hr_email], fail_silently=False,
                )
                EmailLog.objects.create(recipient=hr_email, subject=f"Application access denied - #{app.id}", success=True)
            except Exception as exc:
                EmailLog.objects.create(recipient=hr_email, subject=f"Application access denied - #{app.id}", success=False, error=str(exc))

        return render(request, "core/application-access-approval.html", {
            "access_request": access_request, "application": app, "candidate": candidate, "denied_now": True,
        })

    return render(request, "core/application-access-approval.html", {
        "access_request": access_request, "application": app, "candidate": candidate,
    })


def _application_pdf_response(app):
    from .application_pdf import build_application_pdf

    context = _application_display_context(app)
    context.update({
        "application": app,
        "uploaded_files": list(app.files.all().order_by("-uploaded_at")),
    })
    return build_application_pdf(app, context)


def _stringify_app_value(value):
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [_stringify_app_value(item) for item in value]
        return ", ".join(part for part in parts if part)
    if isinstance(value, dict):
        return ""
    text = str(value).strip()
    if text.lower() in {"please select", "select"}:
        return ""
    return text


class SafeDisplayDict(dict):
    """Template-safe mapping: missing keys render as empty, not as an error."""

    def __missing__(self, key):
        return ""


def _flatten_application_data(data):
    flat = SafeDisplayDict()
    if not isinstance(data, dict):
        return flat

    def walk(obj):
        if not isinstance(obj, dict):
            return
        for key, val in obj.items():
            if isinstance(val, dict):
                walk(val)
            else:
                flat[key] = _stringify_app_value(val)

    walk(data)
    return flat


def _section_values(data, name):
    raw = data.get(name) if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return SafeDisplayDict()
    return SafeDisplayDict(
        {
            key: _stringify_app_value(val)
            for key, val in raw.items()
            if not isinstance(val, dict)
        }
    )


def _yes_no_details(answer, details):
    answer = (answer or "").strip()
    details = (details or "").strip()
    if answer.lower() == "yes" and details:
        return f"Yes, {details}"
    return answer


def _files_by_field(app):
    mapping = SafeDisplayDict()
    for uploaded in app.files.all().order_by("uploaded_at", "id"):
        mapping[uploaded.field_name] = uploaded
    return mapping


def _occupation_label(flat):
    parts = [
        flat.get(f"macro_1_checkbox_{index}")
        for index in range(1, 9)
        if flat.get(f"macro_1_checkbox_{index}")
    ]
    if parts:
        return ", ".join(parts)
    position = flat.get("position_applied") or ""
    others = flat.get("others") or ""
    if position and others and "other" in position.lower():
        return f"{position} ({others})"
    return position or others


def _residence_label(flat):
    parts = [
        flat.get(key)
        for key in (
            "macro_1_checkbox_9",
            "macro_1_checkbox_10",
            "macro_1_checkbox_11",
            "macro_1_checkbox_12",
        )
        if flat.get(key)
    ]
    extra = flat.get("others_2") or ""
    if extra:
        parts.append(extra)
    return ", ".join(parts)


def _last_nonempty_part(value):
    text = (value or "").strip()
    if not text:
        return ""
    parts = [part.strip() for part in text.split(",") if part.strip()]
    return parts[-1] if parts else text


def _application_display_context(app):
    data = app.data if isinstance(app.data, dict) else {}
    fields = _flatten_application_data(data)
    section3 = _section_values(data, "macro_section_3")
    fields["lang_other"] = (
        section3.get("lang_other")
        or _last_nonempty_part(section3.get("others"))
        or fields.get("lang_other")
    )
    fields["lang_other_2"] = (
        section3.get("lang_other_2")
        or _last_nonempty_part(section3.get("others_2"))
        or fields.get("lang_other_2")
    )
    personal = _section_values(data, "personal_information")
    personal["medical_display"] = _yes_no_details(
        personal.get("medical_condition"),
        personal.get("medical_condition_details"),
    )
    personal["criminal_display"] = _yes_no_details(
        personal.get("criminal_offence"),
        personal.get("criminal_offence_details"),
    )
    return {
        "fields": fields,
        "pi": personal,
        "iq": _section_values(data, "iq_test"),
        "consent": _section_values(data, "consent"),
        "emp": _section_values(data, "employment_expectations"),
        "ref": _section_values(data, "reference"),
        "decl": _section_values(data, "declaration"),
        "files_by_field": _files_by_field(app),
        "occupation_label": _occupation_label(fields),
        "residence_label": _residence_label(fields),
    }


def _valid_forward_access(request, app):
    """Validate a secure HR forwarding session for one application."""
    if get_role(request.user) != "hr":
        return False
    token_value = request.session.get(f"forward_access_{app.pk}", "")
    if not token_value:
        return False
    invite = (
        AccountInvitation.objects
        .select_related("company")
        .filter(
            token=token_value,
            target_role="hr",
            email__iexact=(request.user.email or "").strip(),
        )
        .first()
    )
    if not invite or not invite.company or not app.company:
        return False
    return invite.company_id == app.company_id


def forwarded_application_entry(request, token, pk):
    """Landing page for an application forwarded to a specific HR email."""
    invitation = get_object_or_404(
        AccountInvitation.objects.select_related("company"),
        token=token,
        target_role="hr",
    )
    app = get_object_or_404(
        JobApplication.objects.select_related("candidate", "company").prefetch_related("files"),
        pk=pk,
    )

    if not invitation.company_id or invitation.company_id != app.company_id:
        raise Http404

    expected_email = (invitation.email or "").strip().lower()

    if not request.user.is_authenticated:
        login_url = f"{reverse('login')}?next={request.build_absolute_uri()}"
        return redirect(login_url)

    if (request.user.email or "").strip().lower() != expected_email:
        messages.error(
            request,
            "Please log in or register with the HR email address that received this application.",
        )
        return redirect("dashboard")

    if get_role(request.user) != "hr":
        messages.error(request, "This forwarded application is available to an HR account only.")
        return redirect("dashboard")

    expected_type = (
        "insitemy" if "insite" in invitation.company.name.lower() else "macro_kiosk"
    )
    if user_application_type(request.user) != expected_type:
        messages.error(
            request,
            f"This application belongs to {invitation.company.name}. "
            "Please use the matching company HR account.",
        )
        return redirect("dashboard")

    request.session[f"forward_access_{app.pk}"] = str(invitation.token)
    request.session.modified = True
    return redirect("application_detail", pk=app.pk)



@role_required("hr", "administration", "agent", "candidate")
def application_detail(request, pk):
    app = get_object_or_404(
        JobApplication.objects
        .select_related("candidate", "company")
        .prefetch_related("files"),
        pk=pk,
    )

    role = get_role(request.user)

    # HR accounts are company-scoped.
    # HR users cannot access applications belonging to other companies.
    if role == "hr":
        expected_company = hr_company_name(request.user)

        if (
            not app.company
            or app.company.name.strip().lower() != expected_company.lower()
        ):
            raise Http404

        # Normal HR access requires approval.
        # A valid, email-bound forwarding link is an exception.
        if not _valid_forward_access(request, app):
            access_request = (
                ApplicationAccessRequest.objects
                .filter(
                    application=app,
                    requested_by=request.user,
                    status="approved",
                )
                .order_by("-approved_at")
                .first()
            )

            if not access_request:
                messages.info(
                    request,
                    "Request Access must be approved by the client "
                    "before this application can be viewed.",
                )
                return redirect("received_applications")

    # Agent access and approval checks.
    if role == "agent":
        if not AgentClient.objects.filter(
            agent=request.user,
            candidate=app.candidate,
        ).exists():
            raise Http404

        was_forwarded = app.audit_trails.filter(
            user=request.user,
            action="forwarded",
        ).exists()

        if (
            was_forwarded
            and not app.access_requests.filter(
                requested_by=request.user,
                status="approved",
            ).exists()
        ):
            messages.info(
                request,
                "The candidate must approve your access request "
                "before you can view this forwarded application.",
            )
            return redirect("submitted_applications")

    # Candidates can access only their own applications.
    if role == "candidate":
        candidate = candidate_for_user(request.user)

        if not candidate or app.candidate_id != candidate.pk:
            raise Http404

    # Record HR and Agent activity only after access checks succeed.
    if role == "hr":
        _record_hr_audit(
            request,
            app,
            (
                "hr_downloaded"
                if request.GET.get("download", "").lower() == "pdf"
                else "hr_viewed"
            ),
        )

    elif role == "agent":
        _record_agent_audit(
            request,
            app,
            (
                "agent_downloaded"
                if request.GET.get("download", "").lower() == "pdf"
                else "agent_viewed"
            ),
        )

    # Normalize the candidate's name for display.
    if app.candidate:
        app.candidate.full_name = normalize_person_name(
            app.candidate.full_name
        )

    # PDF download.
    if request.GET.get("download", "").lower() == "pdf":
        return _application_pdf_response(app)

    # Select the application template based on company name.
    company_name = (
        (app.company.name or "").strip().lower()
        if app.company
        else ""
    )

    if "nevigate" in company_name:
        template = "core/Nevigate-application.html"

    elif "raddish" in company_name:
        template = "core/raddish-application.html"

    elif "insite" in company_name:
        template = "core/insitemy-application.html"

    elif "macro" in company_name:
        template = "core/macrokiosk-application.html"

    else:
        raise Http404(
            "Application company template not found: "
            f"{app.company.name if app.company else 'No company assigned'}"
        )

    
    
    saved_data = app.data if isinstance(app.data, dict) else {}

    # Declaration details expected by the InsiteMy template
    decl = saved_data.get("declaration", {})
    if not isinstance(decl, dict):
       decl = {}
    
    # Support the field name expected by the existing template
    decl.setdefault(
        "appname",
        decl.get("declaration_appname", saved_data.get("consent", {}).get("appname", ""))
    )


    fields = {}

    # Flatten nested MacroKiosk sections, if present.
    for section_name, section_data in saved_data.items():
        if section_name.startswith("macro_section_") and isinstance(section_data, dict):
           fields.update(section_data)

    # Also support applications saved directly as flat dictionaries.
    fields.update({

        key: value
        for key, value in saved_data.items()
        if not key.startswith("macro_section_")
    })

    # Map common template fields to existing saved values.
    fields.setdefault("date", fields.get("macro_5_date") or fields.get("macro_7_date") or fields.get("dateapplied", ""))
    
    # Provide personal information expected by the InsiteMy template.
    pi = saved_data.get("personal_information", {})

    if not isinstance(pi, dict):
       pi = {}

    # Support legacy applications where personal information is stored flat.
    for key in (
        "name", "passportnumber", "nationality", "dob", "age",
        "gender", "marital_status", "residential_address",
        "residential_city", "residential_state", "residential_country",
        "residential_pincode", "permanent_address", "permanent_city",
        "permanent_state", "permanent_country", "permanent_pincode",
        "height", "weight", "bms", "email", "phone",
    ):
        if not pi.get(key):
            pi[key] = fields.get(key, "")

    pi.setdefault("medical_display", pi.get("medical_condition", ""))
    pi.setdefault("criminal_display", pi.get("criminal_offence", ""))
    
    # Prevent missing legacy checkbox fields from breaking application details.
    for number in range(1, 21):
        fields.setdefault(f"macro_4_checkbox_{number}", "")

    



    return render(
        request,
        template,
        {
            "application": app,
            "role": role,
            "saved_data": saved_data,
            "fields": fields,
            "pi": pi,
            "decl": decl,
            "uploaded_files": app.files.all().order_by("-uploaded_at"),
        },
    )



@role_required("agent")
def forward_application_to_hr(request, pk):
    if request.method != "POST":
        return redirect("submitted_applications")

    app = get_object_or_404(
        JobApplication.objects.select_related("candidate", "company"),
        pk=pk,
        status__in={"submitted", "reviewed", "shortlisted", "rejected"},
        archived=False,
    )

    if not AgentClient.objects.filter(
        agent=request.user,
        candidate=app.candidate,
    ).exists():
        raise Http404

    recipient_email = (request.POST.get("recipient_email") or "").strip().lower()
    if not recipient_email:
        messages.error(request, "Please enter the HR email address to forward this application.")
        return redirect(request.POST.get("next") or "received_applications")

    from django.core.validators import validate_email
    try:
        validate_email(recipient_email)
    except ValidationError:
        messages.error(request, "Please enter a valid HR email address.")
        return redirect(request.POST.get("next") or "received_applications")

    # A forwarding invitation is tied to the exact company and recipient email.
    forward_invite = AccountInvitation.objects.create(
        email=recipient_email,
        invited_by=request.user,
        target_role="hr",
        company=app.company,
    )

    entry_url = request.build_absolute_uri(
        reverse(
            "forwarded_application_entry",
            kwargs={"token": forward_invite.token, "pk": app.pk},
        )
    )
    login_url = request.build_absolute_uri(
        reverse("login") + "?next=" + entry_url
    )
    signup_url = request.build_absolute_uri(
        reverse("signup")
        + "?account_invite="
        + str(forward_invite.token)
        + "&next="
        + entry_url
    )

    candidate_name = normalize_person_name(app.candidate.full_name) or "Candidate"
    subject = f"{app.company.name} - Candidate Application Forwarded"
    body = (
        f"Dear HR Team,\n\n"
        f"{request.user.get_full_name() or request.user.email} has forwarded "
        f"the application of {candidate_name} (Application #{app.id}) to you.\n\n"
        f"If you already have an HR account, log in using the invited email address:\n{login_url}\n\n"
        f"If you do not have an account, register using the invited email address:\n{signup_url}\n\n"
        "After login or registration, the application will open automatically.\n\n"
        "Stalwart Business Solutions"
    )

    try:
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [recipient_email],
            fail_silently=False,
        )
        forward_invite.sent_at = timezone.now()
        forward_invite.save(update_fields=["sent_at"])
        EmailLog.objects.create(
            recipient=recipient_email,
            subject=subject,
            success=True,
        )
        Notification.objects.create(
            recipient=request.user,
            application=app,
            title="Application forwarded",
            message=f"Application #{app.id} was forwarded to {recipient_email}.",
            target_url=reverse("submitted_applications"),
        )
        _audit_event(request, app, "forwarded")
        messages.success(request, f"This application is shared to HR ({recipient_email}).")
    except Exception as exc:
        forward_invite.active = False
        forward_invite.save(update_fields=["active"])
        EmailLog.objects.create(
            recipient=recipient_email,
            subject=subject,
            success=False,
            error=str(exc),
        )
        messages.error(request, "The application could not be forwarded. Check the EmailLog for the SMTP error.")

    return redirect(request.POST.get("next") or "received_applications")


@role_required("hr", "administration", "agent", "candidate")
def application_audit_trail(request, pk):
    app = get_object_or_404(
        JobApplication.objects.select_related("candidate", "company"),
        pk=pk,
    )
    role = get_role(request.user)
    if role == "hr":
        expected = hr_company_name(request.user)
        if not app.company or app.company.name.strip().lower() != expected.lower():
            raise Http404
    elif role == "agent":
        if not AgentClient.objects.filter(agent=request.user, candidate=app.candidate).exists():
            raise Http404
    elif role == "candidate":
        candidate = candidate_for_user(request.user)
        if not candidate or app.candidate_id != candidate.pk:
            raise Http404

    events = list(
        app.audit_trails.select_related("user").order_by("created_at")
    )
    access_requests = list(
        app.access_requests.select_related("requested_by").order_by("requested_at")
    )
    return render(request, "core/application-audit-trail.html", {
        "application": app,
        "events": events,
        "access_requests": access_requests,
        "role": role,
    })


@role_required("hr", "administration")
def update_application_status(request, pk):
    """Allow HR/Admin to move an application through its review lifecycle."""
    if request.method != "POST":
        return redirect("application_detail", pk=pk)

    app = get_object_or_404(JobApplication.objects.select_related("company"), pk=pk)
    if get_role(request.user) == "hr":
        expected_company = hr_company_name(request.user)
        if not app.company or app.company.name.strip().lower() != expected_company.lower():
            raise Http404

    status = request.POST.get("status", "").strip().lower()

    allowed_statuses = {
        "submitted",
        "reviewed",
        "shortlisted",
        "rejected",
    }

    if status not in allowed_statuses:
        messages.error(request, "Invalid application status.")
        return redirect("application_detail", pk=pk)

    app.status = status
    app.save(update_fields=["status", "updated_at"])

    status_message = f"Application #{app.id} status changed to {app.get_status_display()}."
    _notify_candidate(app.candidate, "Application status updated", status_message, app, reverse("submitted_applications"))
    _notify_candidate_agents(app.candidate, "Application status updated", status_message, app, reverse("submitted_applications"))
    _notify_role("administration", "Application status updated", status_message, app, reverse("received_applications"))

    messages.success(
        request,
        f"Application status updated to {app.get_status_display()}.",
    )
    return redirect("application_detail", pk=pk)


@role_required("hr", "administration")
def delete_application(request, pk):

    if request.method == "POST":

        app = get_object_or_404(
            JobApplication.objects.select_related("company"),
            pk=pk,
        )

        if get_role(request.user) == "hr":
            expected_company = hr_company_name(request.user)
            if not app.company or app.company.name.strip().lower() != expected_company.lower():
                raise Http404

        app.delete()

        messages.success(
            request,
            "Application deleted successfully.",
        )

    return redirect(
        "received_applications"
    )


@role_required("hr", "administration", "agent")
def candidate_list(request):
    candidates = candidate_queryset_for_user(request.user).select_related("user").order_by("-id")
    return render(request, "core/candidate_list.html", {"candidates": candidates, "role": get_role(request.user)})


@role_required("hr", "administration")
def add_candidate(request):
    if request.method == "POST":
        full_name = request.POST.get("full_name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        phone = request.POST.get("phone", "").strip()
        if not full_name or not email:
            messages.error(request, "Name and email are required.")
            return render(request, "core/candidate_add.html")
        candidate, created = Candidate.objects.get_or_create(email=email, defaults={"full_name": full_name, "phone": phone})
        if not created:
            candidate.full_name = full_name; candidate.phone = phone; candidate.save()
        Subscription.objects.get_or_create(candidate=candidate)
        messages.success(request, "Candidate saved successfully.")
        return redirect("candidate_list")
    return render(request, "core/candidate_add.html")


@role_required("hr", "administration", "agent")
def candidate_detail(request, pk):
    candidate = get_object_or_404(Candidate, pk=pk)
    role = get_role(request.user)
    if role == "agent" and not AgentClient.objects.filter(agent=request.user, candidate=candidate).exists():
        raise Http404

    applications = candidate.applications.select_related("company").order_by("-created_at")
    if role == "hr":
        company_name = hr_company_name(request.user)
        applications = applications.filter(company__name__iexact=company_name)

    return render(
        request,
        "core/candidate-detail.html",
        {"candidate": candidate, "role": role, "visible_applications": applications},
    )


@role_required("hr", "administration")
def delete_candidate(request, pk):
    if request.method == "POST":
        get_object_or_404(Candidate, pk=pk).delete()
        messages.success(request, "Candidate deleted successfully.")
    return redirect("candidate_list")


@login_required(login_url="/login/")
def settings_view(request):
    """Administrative settings: company details, users, assignments and legal content."""

    # ================================================================
    # POST
    # ================================================================
    if request.method == "POST":

        action = request.POST.get("action", "").strip()

        admin_actions = {
            "company", "create_user", "delete_user", "delete_company",
            "assign_agent", "remove_assignment", "update_terms", "update_privacy",
        }
        if action in admin_actions and get_role(request.user) != "administration":
            messages.error(request, "Administrator access is required for this action.")
            return redirect("settings")

        # ============================================================
        # CURRENT USER ACCOUNT SETTINGS
        # Available to every authenticated role.
        # ============================================================
        if action == "account":
            first_name = request.POST.get("first_name", "").strip()
            last_name = request.POST.get("last_name", "").strip()
            email = request.POST.get("email", "").strip().lower()

            if not first_name:
                messages.error(request, "First name is required.")
            elif not email:
                messages.error(request, "Email address is required.")
            elif User.objects.exclude(pk=request.user.pk).filter(email__iexact=email).exists():
                messages.error(request, "That email address is already in use.")
            else:
                request.user.first_name = first_name
                request.user.last_name = last_name
                request.user.email = email
                request.user.username = email
                request.user.save(update_fields=["first_name", "last_name", "email", "username"])
                messages.success(request, "Account settings updated successfully.")
            return redirect("settings")

        # ============================================================
        # COMPANY
        # ============================================================
        if action == "company":

            company_name = request.POST.get(
                "company_name",
                ""
            ).strip()

            company_email = request.POST.get(
                "company_email",
                ""
            ).strip()

            company_phone = request.POST.get(
                "company_phone",
                ""
            ).strip()

            if not company_name:

                messages.error(
                    request,
                    "Company name is required."
                )

            elif Company.objects.filter(
                name__iexact=company_name
            ).exists():

                messages.error(
                    request,
                    f"Company '{company_name}' already exists."
                )

            else:

                Company.objects.create(
                    name=company_name,
                    email=company_email,
                    phone=company_phone,
                )

                messages.success(
                    request,
                    f"{company_name} created successfully."
                )

        # ============================================================
        # CREATE USER
        # ============================================================
        elif action == "create_user":

            name = request.POST.get(
                "name",
                ""
            ).strip()

            email = request.POST.get(
                "email",
                ""
            ).strip().lower()

            password = request.POST.get(
                "newpass",
                ""
            )

            confirm_password = request.POST.get(
                "confirmnewpass",
                ""
            )

            role = request.POST.get(
                "role",
                "candidate"
            ).strip().lower()

            allowed_roles = {
                "candidate",
                "agent",
                "hr",
                "administration",
            }

            if role not in allowed_roles:

                messages.error(
                    request,
                    "Please select a valid role."
                )

            elif not name:

                messages.error(
                    request,
                    "Name is required."
                )

            elif not email:

                messages.error(
                    request,
                    "Email address is required."
                )

            elif len(password) < 8:

                messages.error(
                    request,
                    "Password must contain at least 8 characters."
                )

            elif password != confirm_password:

                messages.error(
                    request,
                    "Passwords do not match."
                )

            elif User.objects.filter(
                email__iexact=email
            ).exists():

                messages.error(
                    request,
                    "A user with this email already exists."
                )

            elif User.objects.filter(
                username__iexact=email
            ).exists():

                messages.error(
                    request,
                    "A user with this username already exists."
                )

            elif role == "hr" and not request.POST.get("company", "").strip():

                messages.error(
                    request,
                    "Please select the company for the HR account."
                )

            else:

                with transaction.atomic():

                    user = User.objects.create_user(
                        username=email,
                        email=email,
                        password=password,
                        first_name=name,
                    )

                    # Administration users are staff
                    if role == "administration":

                        user.is_staff = True

                        user.save(
                            update_fields=[
                                "is_staff"
                            ]
                        )

                    # Remove old role groups if any
                    user.groups.clear()

                    group, _ = Group.objects.get_or_create(
                        name=role
                    )

                    user.groups.add(group)

                    profile, _ = UserProfile.objects.get_or_create(user=user)

                    if role == "hr":
                        company_id = request.POST.get("company", "").strip()
                        selected_company = Company.objects.filter(
                            pk=company_id,
                            is_active=True,
                        ).first()

                        if not selected_company:
                            raise ValidationError(
                                "Invalid company selected for HR account."
                            )

                        profile.application_type = (
                            "insitemy"
                            if selected_company.name.strip().lower() == "insitemy"
                            else "macro_kiosk"
                        )
                        profile.save(
                            update_fields=[
                                "application_type",
                                "updated_at",
                            ]
                        )

                    # Candidate profile
                    if role == "candidate":

                        candidate, _ = Candidate.objects.get_or_create(
                            user=user,
                            defaults={
                                "full_name": name,
                                "email": email,
                            },
                        )

                        # If candidate already exists, keep details updated
                        if (
                            candidate.full_name != name
                            or candidate.email != email
                        ):

                            candidate.full_name = name
                            candidate.email = email

                            candidate.save(
                                update_fields=[
                                    "full_name",
                                    "email",
                                ]
                            )

                        Subscription.objects.get_or_create(
                            candidate=candidate
                        )

                messages.success(
                    request,
                    f"{name} was created successfully as {role.title()}."
                )

        # ============================================================
        # DELETE USER
        # ============================================================
        elif action == "delete_user":

            user_id = request.POST.get(
                "user_id"
            )

            try:

                user = User.objects.get(
                    pk=user_id
                )

                if user.pk == request.user.pk:
                    messages.error(request, "You cannot delete your own logged-in account.")
                elif get_role(user) == "administration":
                    active_admins = [
                        admin for admin in User.objects.filter(is_active=True)
                        if get_role(admin) == "administration"
                    ]
                    if len(active_admins) <= 1:
                        messages.error(request, "The last administrator account cannot be deleted.")
                    else:
                        user.delete()
                        messages.success(request, "User deleted successfully.")
                else:
                    user.delete()
                    messages.success(request, "User deleted successfully.")

            except User.DoesNotExist:

                messages.error(
                    request,
                    "User not found."
                )

        # ============================================================
        # DELETE COMPANY
        # ============================================================
        elif action == "delete_company":

            company_id = request.POST.get(
                "company_id"
            )

            try:

                company = Company.objects.get(
                    pk=company_id
                )

                if JobApplication.objects.filter(
                    company=company
                ).exists():

                    messages.error(
                        request,
                        f"Company '{company.name}' cannot be deleted because it has existing job applications."
                    )

                else:

                    company.delete()

                    messages.success(
                        request,
                        f"Company '{company.name}' deleted successfully."
                    )

            except Company.DoesNotExist:

                messages.error(
                    request,
                    "Company not found."
                )

        # ============================================================
        # ASSIGN AGENT TO CANDIDATE
        # ============================================================
        elif action == "assign_agent":

            agent_id = request.POST.get(
                "agent_id"
            )

            # Support both single and multiple candidate fields
            candidate_id = request.POST.get(
                "candidate_id"
            )

            candidate_ids = request.POST.getlist(
                "candidate_ids[]"
            )

            if not candidate_ids and candidate_id:
                candidate_ids = [candidate_id]

            if not agent_id:

                messages.error(
                    request,
                    "Please select an agent."
                )

            elif not candidate_ids:

                messages.error(
                    request,
                    "Please select at least one candidate."
                )

            else:

                try:

                    agent = User.objects.get(
                        pk=agent_id
                    )

                    if get_role(agent) != "agent":

                        raise ValueError(
                            "Selected user is not an agent."
                        )

                    valid_count = 0

                    for selected_candidate_id in candidate_ids:

                        try:

                            candidate = Candidate.objects.get(
                                pk=selected_candidate_id
                            )

                            AgentClient.objects.get_or_create(
                                agent=agent,
                                candidate=candidate,
                            )

                            valid_count += 1

                        except Candidate.DoesNotExist:

                            continue

                    if valid_count:

                        messages.success(
                            request,
                            f"{valid_count} candidate(s) assigned successfully."
                        )

                    else:

                        messages.error(
                            request,
                            "No valid candidates were selected."
                        )

                except User.DoesNotExist:

                    messages.error(
                        request,
                        "Selected agent was not found."
                    )

                except ValueError:

                    messages.error(
                        request,
                        "Please select a valid agent."
                    )

        # ============================================================
        # REMOVE ASSIGNMENT
        # ============================================================
        elif action == "remove_assignment":

            assignment_id = request.POST.get(
                "assignment_id"
            )

            deleted, _ = AgentClient.objects.filter(
                pk=assignment_id
            ).delete()

            if deleted:

                messages.success(
                    request,
                    "Agent assignment removed successfully."
                )

            else:

                messages.error(
                    request,
                    "Assignment not found."
                )

        # ============================================================
        # TERMS
        # ============================================================
        elif action == "update_terms":

            terms_content = request.POST.get(
                "terms_content",
                ""
            ).strip()

            AppSetting.objects.update_or_create(
                key="terms_content",
                defaults={
                    "value": terms_content
                },
            )

            messages.success(
                request,
                "Terms and Conditions updated successfully."
            )

        # ============================================================
        # PRIVACY
        # ============================================================
        elif action == "update_privacy":

            privacy_content = request.POST.get(
                "privacy_content",
                ""
            ).strip()

            AppSetting.objects.update_or_create(
                key="privacy_content",
                defaults={
                    "value": privacy_content
                },
            )

            messages.success(
                request,
                "Privacy Policy updated successfully."
            )

        # ============================================================
        # INVALID ACTION
        # ============================================================
        else:

            messages.error(
                request,
                "Invalid settings action."
            )

        # ============================================================
        # AFTER POST
        # ============================================================
        return redirect("settings")

    # ================================================================
    # LOAD APP SETTINGS
    # ================================================================
    settings_map = {
        item.key: item.value
        for item in AppSetting.objects.filter(
            key__in=[
                "company_name",
                "company_email",
                "company_phone",
                "terms_content",
                "privacy_content",
            ]
        )
    }

    # ================================================================
    # LOAD COMPANIES
    # ================================================================
    companies = Company.objects.all().order_by("-id")

    # ================================================================
    # LOAD USERS
    # ================================================================
    users = (
        User.objects
        .prefetch_related("groups")
        .all()
        .order_by("-date_joined")
    )

    # ================================================================
    # LOAD AGENTS
    # ================================================================
    agents = [
        user
        for user in users
        if get_role(user) == "agent"
    ]

    # ================================================================
    # LOAD CANDIDATES
    # ================================================================
    candidates = (
        Candidate.objects
        .select_related("user")
        .all()
        .order_by("-id")
    )

    # ================================================================
    # LOAD ASSIGNMENTS
    # ================================================================
    assignments = (
        AgentClient.objects
        .select_related(
            "agent",
            "candidate",
        )
        .order_by("-created_at")
    )

    # ================================================================
    # RENDER
    # ================================================================
    return render(
        request,
        "core/settings.html",
        {
            "role": get_role(request.user),

            "settings_map": settings_map,

            "companies": companies,

            "users": users,

            "agents": agents,

            "candidates": candidates,

            "assignments": assignments,
        },
    )

@login_required
def myprofile(request):
    """Display and update the authenticated user's My Profile data."""
    candidate = candidate_for_user(request.user)
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    employment_records = (
        EmploymentHistory.objects.filter(candidate=candidate).order_by("-id")
        if candidate else EmploymentHistory.objects.none()
    )
    employment_history = employment_records.first()

    if request.method == "POST":
        section = request.POST.get("profile_section", "").strip().lower()
        if not section:
            if any(k in request.POST for k in ["profile_option_14","profile_option_15","profile_option_16","profile_option_17","profile_option_18","profile_option_19","profile_option_20","profile_option_21","position_others"]):
                section = "position"
            elif any(k in request.POST for k in ["residential_address","residential_city","residential_state","residential_country","residential_pincode","permanent_address","permanent_city","permanent_state","permanent_country","permanent_pincode","permanent_same_as_residential"]):
                section = "address"
            elif any(k in request.POST for k in ["institution","education_location","yearfrom","yearto","qualification"]):
                section = "education"
            elif "lang" in request.POST:
                section = "language"

        # --------------------------------------------------------
        # PROFILE PHOTO
        # --------------------------------------------------------
        uploaded_photo = request.FILES.get("profile_photo") or request.FILES.get("file")
        if uploaded_photo:
            if uploaded_photo.size > 5 * 1024 * 1024:
                messages.error(request, "Profile image must be 5 MB or smaller.")
                return redirect("myprofile")

            if Path(uploaded_photo.name).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                messages.error(request, "Profile image must be JPG, JPEG, PNG or WEBP.")
                return redirect("myprofile")

            profile.profile_photo = uploaded_photo

        # --------------------------------------------------------
        # EMPLOYMENT HISTORY
        # --------------------------------------------------------
        employment_fields = {
            "employer_name", "employment_location", "superior", "position",
            "ssalary", "esalary", "reasons", "job_description",
            "employment_date_from", "employment_date_to",
        }

        if section == "employment" or employment_fields.intersection(request.POST.keys()):
            if not candidate:
                messages.error(request, "Candidate profile was not found.")
                return redirect("myprofile")

            from datetime import date
            from decimal import Decimal, InvalidOperation

            def parse_date(value):
                value = (value or "").strip()
                if not value:
                    return None
                try:
                    return date.fromisoformat(value)
                except (TypeError, ValueError):
                    return None

            def parse_decimal(value):
                value = (value or "").strip()
                if not value:
                    return None
                try:
                    return Decimal(value)
                except (InvalidOperation, ValueError, TypeError):
                    return None

            employment_id = request.POST.get("employment_id", "").strip()

            if employment_id:
                try:
                    employment = EmploymentHistory.objects.get(
                        pk=int(employment_id),
                        candidate=candidate,
                    )
                except (EmploymentHistory.DoesNotExist, ValueError, TypeError):
                    messages.error(request, "The selected employment record was not found.")
                    return redirect("myprofile")
            else:
                employment = EmploymentHistory(candidate=candidate)

            employment.date_from = parse_date(request.POST.get("employment_date_from"))
            employment.date_to = parse_date(request.POST.get("employment_date_to"))
            employment.employer_name = request.POST.get("employer_name", "").strip()
            employment.location = request.POST.get("employment_location", "").strip()
            employment.immediate_superior = request.POST.get("superior", "").strip()
            employment.position = request.POST.get("position", "").strip()
            employment.start_salary = parse_decimal(request.POST.get("ssalary"))
            employment.end_salary = parse_decimal(request.POST.get("esalary"))
            employment.reason_for_leaving = request.POST.get("reasons", "").strip()
            employment.job_description = request.POST.get("job_description", "").strip()
            employment.save()

            profile.save()
            messages.success(request, "Employment history updated successfully.")
            return redirect("myprofile")

        # --------------------------------------------------------
        # YOUR INFORMATION / ACCOUNT
        # --------------------------------------------------------
        first_name = request.POST.get("fname")
        middle_name = request.POST.get("mname")
        last_name = request.POST.get("lname")
        email = request.POST.get("email")
        phone = request.POST.get("phone")

        if email is not None:
            email = email.strip().lower()
            if not email:
                messages.error(request, "Email address is required.")
                return redirect("myprofile")

            if User.objects.exclude(pk=request.user.pk).filter(email__iexact=email).exists():
                messages.error(request, "That email address is already in use.")
                return redirect("myprofile")

            request.user.email = email
            request.user.username = email

        if first_name is not None:
            request.user.first_name = first_name.strip()

        if last_name is not None:
            request.user.last_name = last_name.strip()

        request.user.save()

        if middle_name is not None:
            profile.middle_name = middle_name.strip()

        if phone is not None:
            profile.phone = phone.strip()

        # --------------------------------------------------------
        # OTHER PROFILE MODALS -> JSON
        # --------------------------------------------------------
        profile_data = dict(profile.profile_data or {})

        section_keys = {
            "position": [f"profile_option_{i}" for i in range(1, 22)] + ["position_others"],
            "personal": ["passportnumber", "nationality", "dob", "age", "gender", "marital"],
            "family": ["father", "mother", "spouse", "children", "brothers", "sisters"],
            "address": [
                "address_option_house", "address_option_parents", "address_option_rented",
                "address_option_other", "address_others", "residential_address",
                "residential_city", "residential_state", "residential_country",
                "residential_pincode", "permanent_same_as_residential", "permanent_address",
                "permanent_city", "permanent_state", "permanent_country", "permanent_pincode",
            ],
            "health": ["height", "weight", "bms"],
            "expectations": ["willing_to_travel", "willing_to_relocate", "own_transport", "lastsalary", "expectedsalary", "notice"],
            "education": ["institution", "education_location", "yearfrom", "yearto", "qualification"],
            "language": ["lang"],
            "activities": ["activities"],
            "references": ["ref1name", "ref1pos", "ref1com", "ref1email", "ref1phn", "ref2name", "ref2pos", "ref2com", "ref2email", "ref2phn"],
        }

        for key in section_keys.get(section, []):
            profile_data.pop(key, None)

        ignored = {
            "csrfmiddlewaretoken", "profile_section", "fname", "mname", "lname",
            "email", "phone", "employment_id", "save_section", "save_profile",
        } | employment_fields

        for key, values in request.POST.lists():
            if key in ignored or key.startswith("save_"):
                continue
            profile_data[key] = values[0] if len(values) == 1 else values

        if section == "position":
            labels = {
                "profile_option_14": "Analyst Programmer (Java / .Net)",
                "profile_option_15": "Product / Project Manager",
                "profile_option_16": "Business Manager / Sales / Marketing",
                "profile_option_17": "Finance / HR / Admin",
                "profile_option_18": "RPA Developer",
                "profile_option_19": "Consultant",
                "profile_option_20": "Designer",
                "profile_option_21": "Others",
            }
            selected = next((k for k in labels if profile_data.get(k)), None)
            profile_data["position_selected"] = (
                (profile_data.get("position_others") or "").strip() if selected == "profile_option_21"
                else labels[selected]
            ) if selected else ""

        if section == "address" and request.POST.get("permanent_same_as_residential"):
            profile_data["permanent_address"] = profile_data.get("residential_address", "")
            profile_data["permanent_city"] = profile_data.get("residential_city", "")
            profile_data["permanent_state"] = profile_data.get("residential_state", "")
            profile_data["permanent_country"] = profile_data.get("residential_country", "")
            profile_data["permanent_pincode"] = profile_data.get("residential_pincode", "")

        profile.profile_data = profile_data
        profile.save()

        if candidate:
            if first_name is not None or last_name is not None:
                candidate.full_name = " ".join(
                    value for value in [request.user.first_name, request.user.last_name] if value
                ).strip()
            if email is not None:
                candidate.email = email
            if phone is not None:
                candidate.phone = phone.strip()

            candidate.profile_data = profile_data
            candidate.save()

        messages.success(request, "Your profile has been updated successfully.")
        return redirect("myprofile")

    # Profile JSON from both sources. UserProfile values take precedence.
    profile_data = dict(candidate.profile_data or {}) if candidate else {}
    profile_data.update(profile.profile_data or {})
    if not profile_data.get("position_selected"):
        labels = {"profile_option_14":"Analyst Programmer (Java / .Net)","profile_option_15":"Product / Project Manager","profile_option_16":"Business Manager / Sales / Marketing","profile_option_17":"Finance / HR / Admin","profile_option_18":"RPA Developer","profile_option_19":"Consultant","profile_option_20":"Designer","profile_option_21":"Others"}
        selected = next((k for k in labels if profile_data.get(k)), None)
        if selected:
            profile_data["position_selected"] = profile_data.get("position_others") or labels[selected] if selected == "profile_option_21" else labels[selected]

    return render(request, "core/myprofile.html", {
        "candidate": candidate,
        "profile": profile,
        "employment_history": employment_history,
        "employment_records": employment_records,
        "profile_data": profile_data,
        "display_phone": profile.phone,
    })


@role_required("administration", "hr", "agent", "candidate")
def security(request):
    if request.method == "POST":
        old_password = request.POST.get("oldpass", "")
        new_password = request.POST.get("newpass", "")
        confirm = request.POST.get("confirmnewpass", "")
        if not request.user.check_password(old_password):
            messages.error(request, "Old password is incorrect.")
        elif len(new_password) < 8:
            messages.error(request, "New password must be at least 8 characters long.")
        elif new_password != confirm:
            messages.error(request, "New passwords do not match.")
        else:
            request.user.set_password(new_password)
            request.user.save(update_fields=["password"])
            update_session_auth_hash(request, request.user)
            messages.success(request, "Password updated successfully.")
            return redirect("security")
    return render(request, "core/security.html", {"role": get_role(request.user)})


@role_required("candidate")
def mydata(request):
    candidate = candidate_for_user(request.user)
    if not candidate:
        messages.error(request, "Candidate profile was not found.")
        return redirect("dashboard")
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "upload":
            name = request.POST.get("dcname", "").strip()
            uploaded = request.FILES.get("document")
            if not name or not uploaded:
                messages.error(request, "Document name and file are required.")
            elif uploaded.size > MAX_UPLOAD_SIZE:
                messages.error(request, "Maximum file size is 1 MB.")
            elif Path(uploaded.name).suffix.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
                messages.error(request, "This file type is not allowed.")
            else:
                CandidateDocument.objects.create(candidate=candidate, name=name, file=uploaded)
                messages.success(request, "Document uploaded successfully.")
        elif action == "delete":
            CandidateDocument.objects.filter(pk=request.POST.get("document_id"), candidate=candidate).delete()
            messages.success(request, "Document deleted successfully.")
        return redirect("mydata")
    return render(
        request,
        "core/mydata.html",
        {"role": get_role(request.user), "candidate": candidate, "documents": candidate.documents.order_by("-uploaded_at")},
    )


def simple_page(request, page):
    allowed = {
        "faqs": "faqs.html",
        "terms": "terms-conditions.html",
        "privacy": "privacy-policy.html",
        "feedback": "feedback.html",
    }
    if page not in allowed:
        raise Http404
    if not request.user.is_authenticated:
        return redirect(f"/login/?next={request.path}")
    if page == "feedback" and request.method == "POST":
        message = request.POST.get("message", "").strip()
        try:
            rating = max(0, min(int(request.POST.get("rating", "0")), 5))
        except ValueError:
            rating = 0
        if not message:
            messages.error(request, "Please enter your feedback message.")
        else:
            Feedback.objects.create(
                candidate=candidate_for_user(request.user),
                message=message,
                rating=rating,
            )
            messages.success(request, "Feedback submitted successfully.")
            return redirect("feedback")
    return render(request, "core/" + allowed[page], {"role": get_role(request.user)})