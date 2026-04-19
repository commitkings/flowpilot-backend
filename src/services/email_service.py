"""Email delivery via Resend API.

Templates live in src/templates/emails/ and are rendered with Jinja2.
All public send_* functions are fire-and-log — they return True/False but never raise.
"""

import base64
import csv
import io
import logging
from pathlib import Path
from typing import Optional

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.config.settings import Settings

logger = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"

# ── Jinja2 environment pointing at our email templates ────────────────────────

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "emails"

_jinja_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _render(template_name: str, **context: object) -> str:
    """Render a Jinja2 email template with the given context variables."""
    template = _jinja_env.get_template(template_name)
    return template.render(**context)


# ── Low-level HTTP sender ─────────────────────────────────────────────────────


async def _send(
    to: str,
    subject: str,
    html: str,
    attachments: Optional[list[dict]] = None,
) -> bool:
    """POST to Resend API.  Returns True on success, False on any failure.

    attachments format: [{"filename": "name.csv", "content": "<base64>"}]
    """
    api_key = Settings.get_resend_api_key()
    if not api_key:
        logger.warning("RESEND_API_KEY not configured — skipping email to %s", to)
        logger.info("Would send | subject=%r to=%s", subject, to)
        return False

    payload: dict = {
        "from": Settings.DEFAULT_FROM_EMAIL,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if attachments:
        payload["attachments"] = attachments

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if resp.status_code in (200, 201):
            logger.info("Email sent | subject=%r to=%s", subject, to)
            return True

        logger.error(
            "Resend API error %d | to=%s body=%s",
            resp.status_code,
            to,
            resp.text[:300],
        )
        return False

    except Exception as exc:
        logger.error("Email delivery failed | to=%s error=%s", to, exc)
        return False


# ── Notification preference helper ───────────────────────────────────────────


def check_notification_pref(user, key: str) -> bool:
    """Return True if `key` is enabled in the user's notification preferences.

    Defaults to True when the key is absent or preferences are null — preserving
    existing behaviour for users who have never set their preferences.
    """
    prefs = getattr(user, "notification_preferences", None) or {}
    return bool(prefs.get(key, True))


# ── Public send functions ─────────────────────────────────────────────────────


async def send_team_invite_email(
    to: str,
    business_name: str,
    inviter_name: str,
    role: str,
    accept_url: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Send a magic-link invitation to someone who doesn't have an account yet.

    Template: src/templates/emails/team_invite.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    html = _render(
        "team_invite.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        business_name=business_name,
        inviter_name=inviter_name,
        role_display=role.capitalize(),
        accept_url=accept_url,
    )
    return await _send(
        to=to,
        subject=f"You're invited to join {business_name} on FlowPilot",
        html=html,
    )


async def send_team_added_email(
    to: str,
    business_name: str,
    inviter_name: str,
    role: str,
    dashboard_url: Optional[str] = None,
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify an existing user that they've been added to an organisation.

    Template: src/templates/emails/team_added.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    html = _render(
        "team_added.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        business_name=business_name,
        inviter_name=inviter_name,
        role_display=role.capitalize(),
        dashboard_url=dashboard_url or f"{base}/dashboard",
    )
    return await _send(
        to=to,
        subject=f"You've been added to {business_name} on FlowPilot",
        html=html,
    )


async def send_welcome_email(
    to: str,
    display_name: str,
    business_name: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Send a welcome email after an organisation completes onboarding.

    Template: src/templates/emails/welcome.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    # Use first name only if we can split, fall back to full display name
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "welcome.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        business_name=business_name,
        dashboard_url=f"{base}/dashboard",
    )
    return await _send(
        to=to,
        subject=f"Welcome to FlowPilot, {first_name} — {business_name} is live!",
        html=html,
    )


async def send_run_awaiting_approval_email(
    to: str,
    run_id: str,
    objective: str,
    candidate_count: int,
    approver_name: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify an approver/owner that a payout run needs their review.

    Template: src/templates/emails/run_awaiting_approval.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    approve_url = f"{base}/dashboard/runs/{run_id}/approve"
    html = _render(
        "run_awaiting_approval.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        approver_name=approver_name.split()[0] if approver_name else "there",
        objective=objective,
        candidate_count=candidate_count,
        approve_url=approve_url,
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    return await _send(
        to=to,
        subject=f"Action required: {candidate_count} payout candidate{'s' if candidate_count != 1 else ''} awaiting your approval",
        html=html,
    )


async def send_run_completed_email(
    to: str,
    run_id: str,
    objective: str,
    status: str,
    approved_count: int,
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify the run creator that their payout run has completed or failed.

    Template: src/templates/emails/run_completed.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    run_url = f"{base}/dashboard/runs/{run_id}"
    html = _render(
        "run_completed.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        objective=objective,
        status=status,
        approved_count=approved_count,
        run_url=run_url,
        is_success=status == "completed",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    subject = (
        f"Your payout run completed — {approved_count} transaction{'s' if approved_count != 1 else ''} processed"
        if status == "completed"
        else "Your payout run encountered an error"
    )
    return await _send(to=to, subject=subject, html=html)


async def send_verification_email(
    to: str,
    code: str,
    display_name: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Send a 6-digit email verification code to a newly registered user.

    Template: src/templates/emails/verify_email.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "verify_email.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        code=code,
    )
    return await _send(
        to=to,
        subject="Your FlowPilot verification code",
        html=html,
    )


def _build_csv_attachment(rows: list[dict], filename: str) -> dict:
    """Serialise transaction rows to a base64-encoded CSV attachment dict."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Reference", "Status", "Amount", "Currency", "Channel", "Direction", "Counterparty", "Bank", "Date"])
    for r in rows:
        writer.writerow([
            r.get("reference", ""),
            r.get("status", ""),
            r.get("amount", ""),
            r.get("currency", "NGN"),
            r.get("channel", ""),
            r.get("direction", ""),
            r.get("counterparty_name", ""),
            r.get("counterparty_bank", ""),
            (r.get("date") or "")[:10],
        ])
    encoded = base64.b64encode(buf.getvalue().encode()).decode()
    return {"filename": filename, "content": encoded}


async def send_transaction_export_email(
    to: str,
    exported_by: str,
    rows: list[dict],
    fmt: str = "csv",
    pdf_base64: Optional[str] = None,
    frontend_url: Optional[str] = None,
) -> bool:
    """Send a transaction export as an email attachment (CSV or PDF).

    Template: src/templates/emails/transaction_export.html
    For PDF, the caller must supply the pre-rendered pdf_base64 string.
    """
    import datetime as _dt

    base = frontend_url or Settings.FRONTEND_URL
    total_volume = sum(r.get("amount", 0) for r in rows)
    date_str = _dt.date.today().isoformat()

    if fmt == "pdf" and pdf_base64:
        filename = f"transactions-{date_str}.pdf"
        attachment = {"filename": filename, "content": pdf_base64}
    else:
        filename = f"transactions-{date_str}.csv"
        attachment = _build_csv_attachment(rows, filename)

    html = _render(
        "transaction_export.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        row_count=len(rows),
        total_volume=f"{total_volume:,.2f}",
        exported_by=exported_by,
        exported_at=_dt.datetime.now().strftime("%d %b %Y, %I:%M %p"),
        filename=filename,
        dashboard_url=f"{base}/dashboard/transactions",
    )
    return await _send(
        to=to,
        subject=f"Your FlowPilot transaction export — {len(rows)} record{'s' if len(rows) != 1 else ''}",
        html=html,
        attachments=[attachment],
    )


async def send_audit_export_email(
    to: str,
    exported_by: str,
    entries: list[dict],
    fmt: str = "csv",
    pdf_base64: Optional[str] = None,
    frontend_url: Optional[str] = None,
) -> bool:
    """Send an audit log export as an email attachment (CSV or PDF).

    Template: src/templates/emails/audit_export.html
    """
    import datetime as _dt

    base = frontend_url or Settings.FRONTEND_URL
    date_str = _dt.date.today().isoformat()

    # Compute date range from entries
    dates = [e.get("created_at", "")[:10] for e in entries if e.get("created_at")]
    if dates:
        date_range = f"{min(dates)} – {max(dates)}" if min(dates) != max(dates) else min(dates)
    else:
        date_range = date_str

    if fmt == "pdf" and pdf_base64:
        filename = f"audit-log-{date_str}.pdf"
        attachment = {"filename": filename, "content": pdf_base64}
    else:
        # Build CSV
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["ID", "Agent", "Action", "Run ID", "Timestamp"])
        for e in entries:
            writer.writerow([
                e.get("id", ""),
                e.get("agent_type", ""),
                e.get("action", ""),
                e.get("run_id", ""),
                (e.get("created_at") or "")[:19],
            ])
        filename = f"audit-log-{date_str}.csv"
        attachment = {
            "filename": filename,
            "content": base64.b64encode(buf.getvalue().encode()).decode(),
        }

    html = _render(
        "audit_export.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        entry_count=len(entries),
        date_range=date_range,
        exported_by=exported_by,
        exported_at=_dt.datetime.now().strftime("%d %b %Y, %I:%M %p"),
        filename=filename,
        dashboard_url=f"{base}/dashboard/audit",
    )
    return await _send(
        to=to,
        subject=f"Your FlowPilot audit log export — {len(entries)} entr{'ies' if len(entries) != 1 else 'y'}",
        html=html,
        attachments=[attachment],
    )


async def send_password_reset_email(
    to: str,
    reset_url: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Send a password reset link.

    Template: src/templates/emails/password_reset.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    html = _render(
        "password_reset.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        reset_url=reset_url,
    )
    return await _send(
        to=to,
        subject="Reset your FlowPilot password",
        html=html,
    )


async def send_account_locked_email(
    to: str,
    display_name: str,
    lock_minutes: int = 10,
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify the user that their account was temporarily locked after too many failed login attempts."""
    base = frontend_url or Settings.FRONTEND_URL
    html = _render(
        "account_locked.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        display_name=display_name,
        lock_minutes=lock_minutes,
        reset_url=f"{base}/forgot-password",
    )
    return await _send(
        to=to,
        subject="Your FlowPilot account has been temporarily locked",
        html=html,
    )


# ── 2FA emails ────────────────────────────────────────────────────────────────


async def send_2fa_enabled_email(
    to: str,
    display_name: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Security alert: 2FA was enabled on the account."""
    base = frontend_url or Settings.FRONTEND_URL
    html = _render(
        "2fa_enabled.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        display_name=display_name,
        settings_url=f"{base}/dashboard/settings",
    )
    return await _send(
        to=to,
        subject="Two-factor authentication enabled — FlowPilot",
        html=html,
    )


async def send_2fa_disabled_email(
    to: str,
    display_name: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Security alert: 2FA was disabled on the account."""
    base = frontend_url or Settings.FRONTEND_URL
    html = _render(
        "2fa_disabled.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        display_name=display_name,
        settings_url=f"{base}/dashboard/settings",
    )
    return await _send(
        to=to,
        subject="Two-factor authentication disabled — FlowPilot",
        html=html,
    )


async def send_2fa_enforced_email(
    to: str,
    display_name: str,
    grace_hours: int = 24,
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify a team member that their org now requires 2FA within a grace period."""
    base = frontend_url or Settings.FRONTEND_URL
    html = _render(
        "2fa_enforced.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        display_name=display_name,
        grace_hours=grace_hours,
        setup_url=f"{base}/dashboard/settings",
    )
    return await _send(
        to=to,
        subject=f"Action required: Set up 2FA within {grace_hours} hours — FlowPilot",
        html=html,
    )


async def send_2fa_grace_expiring_email(
    to: str,
    display_name: str,
    minutes_left: int = 20,
    frontend_url: Optional[str] = None,
) -> bool:
    """Remind a team member that their 2FA grace period is about to expire."""
    base = frontend_url or Settings.FRONTEND_URL
    setup_url = f"{base}/dashboard/settings?tab=security"
    html = _render(
        "2fa_enforced.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        display_name=display_name,
        grace_hours=None,
        grace_minutes=minutes_left,
        setup_url=setup_url,
    )
    return await _send(
        to=to,
        subject=f"⚠️ {minutes_left} minutes left to set up 2FA — FlowPilot",
        html=html,
    )


async def send_api_key_expiry_warning(
    to: str,
    display_name: str,
    key_name: str,
    key_prefix: str,
    days_remaining: int,
    frontend_url: Optional[str] = None,
) -> bool:
    """Warn the key owner that their API key is expiring soon.

    Template: src/templates/emails/api_key_expiry_warning.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    html = _render(
        "api_key_expiry_warning.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        display_name=display_name,
        key_name=key_name,
        key_prefix=key_prefix,
        days_remaining=days_remaining,
        settings_url=f"{base}/dashboard/settings?tab=developer",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    return await _send(
        to=to,
        subject=f"Your API key \"{key_name}\" expires in {days_remaining} day{'s' if days_remaining != 1 else ''} — FlowPilot",
        html=html,
    )


async def send_kyc_submitted_email(
    to: str,
    display_name: str,
    business_name: str,
    submitted_docs: list[str],
    monthly_limit: str = "₦1,500,000",
    single_limit: str = "₦300,000",
    wallet_limit: str = "₦3,000,000",
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify the business owner that KYC documents were received and are under review.

    Template: src/templates/emails/kyc_submitted.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "kyc_submitted.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        business_name=business_name,
        submitted_docs=submitted_docs,
        monthly_limit=monthly_limit,
        single_limit=single_limit,
        wallet_limit=wallet_limit,
        dashboard_url=f"{base}/dashboard",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    return await _send(
        to=to,
        subject=f"KYC documents received — we'll review {business_name} within 10 minutes",
        html=html,
    )


async def send_kyc_verified_email(
    to: str,
    display_name: str,
    business_name: str,
    monthly_limit: str = "₦1,500,000",
    single_limit: str = "₦300,000",
    wallet_limit: str = "₦3,000,000",
    max_monthly_limit: str = "₦50,000,000",
    account_number: Optional[str] = None,
    bank_name: Optional[str] = None,
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify the business owner that their KYC has been approved, with Level 1 limits.

    Template: src/templates/emails/kyc_verified.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "kyc_verified.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        business_name=business_name,
        monthly_limit=monthly_limit,
        single_limit=single_limit,
        wallet_limit=wallet_limit,
        max_monthly_limit=max_monthly_limit,
        account_number=account_number,
        bank_name=bank_name or "your assigned bank",
        dashboard_url=f"{base}/dashboard",
        wallet_url=f"{base}/dashboard/wallet",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    return await _send(
        to=to,
        subject=f"{business_name} is verified on FlowPilot — you're ready to go!",
        html=html,
    )


async def send_individual_kyc_submitted_email(
    to: str,
    display_name: str,
    level: int,
    level_name: str,
    review_time: str = "a few minutes",
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify the individual user that their KYC level submission was received.

    Template: src/templates/emails/kyc_individual_submitted.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "kyc_individual_submitted.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        level=level,
        level_name=level_name,
        review_time=review_time,
        dashboard_url=f"{base}/dashboard",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    level_subjects = {
        1: "Identity verification received — we'll verify your details shortly",
        2: "Address verification received — we're reviewing your details",
        3: "Government ID received — we're reviewing your document",
    }
    subject = level_subjects.get(level, f"Level {level} verification received")
    return await _send(to=to, subject=subject, html=html)


async def send_individual_kyc_verified_email(
    to: str,
    display_name: str,
    level: int,
    level_name: str,
    monthly_limit: str,
    single_limit: str,
    wallet_limit: str,
    at_max_level: bool = False,
    support_email: str = "support@flowpilot.ng",
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify the individual user that their KYC level has been approved and show new limits.

    Template: src/templates/emails/kyc_individual_verified.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "kyc_individual_verified.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        level=level,
        level_name=level_name,
        monthly_limit=monthly_limit,
        single_limit=single_limit,
        wallet_limit=wallet_limit,
        at_max_level=at_max_level,
        support_email=support_email,
        dashboard_url=f"{base}/dashboard",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    level_subjects = {
        1: "Level 1 verified ✓ — You can now send payouts",
        2: "Level 2 verified ✓ — Your monthly limit has been upgraded",
        3: "Level 3 verified ✓ — You've reached the maximum verification level",
    }
    subject = level_subjects.get(level, f"Level {level} verified — your limits have been upgraded")
    return await _send(to=to, subject=subject, html=html)


async def send_scheduled_run_created_email(
    to: str,
    display_name: str,
    schedule_name: str,
    objective: str,
    frequency_label: str,
    next_run_at: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Confirm to the owner that a new scheduled run was created."""
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "scheduled_run_created.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        schedule_name=schedule_name,
        objective=objective,
        frequency_label=frequency_label,
        next_run_at=next_run_at,
        schedules_url=f"{base}/dashboard/runs?tab=scheduled",
    )
    return await _send(
        to=to,
        subject=f"Scheduled run created: \"{schedule_name}\" — FlowPilot",
        html=html,
    )


async def send_scheduled_run_reminder_email(
    to: str,
    display_name: str,
    schedule_name: str,
    objective: str,
    fires_at: str,
    frequency_label: str,
    scheduled_run_id: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify the business owner that a scheduled run fires tomorrow.

    Template: src/templates/emails/scheduled_run_reminder.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "scheduled_run_reminder.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        schedule_name=schedule_name,
        objective=objective,
        fires_at=fires_at,
        frequency_label=frequency_label,
        schedules_url=f"{base}/dashboard/runs?tab=scheduled",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    return await _send(
        to=to,
        subject=f"Reminder: \"{schedule_name}\" runs tomorrow",
        html=html,
    )


async def send_scheduled_run_approval_request_email(
    to: str,
    display_name: str,
    schedule_name: str,
    objective: str,
    fires_at: str,
    frequency_label: str,
    scheduled_run_id: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Send a day-before approval request for a scheduled run.

    Links go to the frontend dashboard — user must log in and enter their approval PIN.
    Template: src/templates/emails/scheduled_run_reminder.html (reused with approval context)
    """
    from src.config.settings import Settings as _Settings
    base = frontend_url or _Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    approve_url = f"{base}/dashboard/runs/scheduled/{scheduled_run_id}"
    skip_url = f"{base}/dashboard/runs/scheduled/{scheduled_run_id}"
    html = _render(
        "scheduled_run_reminder.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        schedule_name=schedule_name,
        objective=objective,
        fires_at=fires_at,
        frequency_label=frequency_label,
        schedules_url=f"{base}/dashboard/runs?tab=scheduled",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
        approve_url=approve_url,
        skip_url=skip_url,
        is_approval_request=True,
    )
    return await _send(
        to=to,
        subject=f"Action required: approve \"{schedule_name}\" before it runs",
        html=html,
    )


async def send_wallet_topup_email(
    to: str,
    display_name: str,
    amount: float,
    new_balance: float,
    reference: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify the business owner that a wallet top-up was successful.

    Template: src/templates/emails/wallet_topup.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "wallet_topup.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        amount=f"{amount:,.2f}",
        new_balance=f"{new_balance:,.2f}",
        reference=reference,
        wallet_url=f"{base}/dashboard/wallet",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    return await _send(
        to=to,
        subject=f"₦{amount:,.2f} added to your FlowPilot wallet",
        html=html,
    )


async def send_wallet_overlimit_email(
    to: str,
    display_name: str,
    new_balance: float,
    wallet_cap: float,
    frontend_url: Optional[str] = None,
) -> bool:
    """Alert the business owner that a deposit has pushed their balance above their KYC wallet cap."""
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    over_by = max(0.0, new_balance - wallet_cap)
    html = _render(
        "wallet_overlimit.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        new_balance=f"{new_balance:,.2f}",
        wallet_cap=f"{wallet_cap:,.2f}",
        over_by=f"{over_by:,.2f}",
        kyc_url=f"{base}/dashboard/settings?tab=kyc",
        wallet_url=f"{base}/dashboard/wallet",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    return await _send(
        to=to,
        subject="Action needed: your wallet balance exceeds your KYC limit",
        html=html,
    )


async def send_wallet_low_balance_email(
    to: str,
    display_name: str,
    balance: float,
    threshold: float,
    frontend_url: Optional[str] = None,
) -> bool:
    """Warn the business owner that their wallet balance is running low.

    Template: src/templates/emails/wallet_low_balance.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "wallet_low_balance.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        balance=f"{balance:,.2f}",
        threshold=f"{threshold:,.2f}",
        wallet_url=f"{base}/dashboard/wallet",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    return await _send(
        to=to,
        subject="Low wallet balance — top up to keep your runs running",
        html=html,
    )


async def send_webhook_unhealthy_email(
    to: str,
    display_name: str,
    webhook_url: str,
    consecutive_failures: int,
    last_failure_at: str,
    last_error: Optional[str] = None,
    frontend_url: Optional[str] = None,
) -> bool:
    """Alert the business owner that a webhook endpoint has failed repeatedly.

    Template: src/templates/emails/webhook_unhealthy.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "webhook_unhealthy.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        webhook_url=webhook_url,
        consecutive_failures=consecutive_failures,
        last_failure_at=last_failure_at,
        last_error=last_error,
        settings_url=f"{base}/dashboard/settings?tab=developer",
    )
    return await _send(
        to=to,
        subject=f"Webhook failing — {consecutive_failures} consecutive delivery errors",
        html=html,
    )


async def send_beneficiary_payment_email(
    to: str,
    org_name: str,
    beneficiary_name: str,
    amount: float,
    account_number: str,
    bank_name: str,
    payment_date: str,
    status: str,
    reference: Optional[str] = None,
    purpose: Optional[str] = None,
    reason: Optional[str] = None,
    business_logo_url: Optional[str] = None,
) -> bool:
    """Notify a beneficiary that a payment was sent (or failed) to their account.

    Template: src/templates/emails/beneficiary_payment.html
    status: "success" | "failed"
    """
    html = _render(
        "beneficiary_payment.html",
        org_name=org_name,
        beneficiary_name=beneficiary_name,
        amount=f"{amount:,.2f}",
        account_number=account_number,
        bank_name=bank_name,
        payment_date=payment_date,
        status=status,
        reference=reference,
        purpose=purpose,
        reason=reason,
        business_logo_url=business_logo_url,
    )
    if status == "success":
        subject = f"You've received ₦{amount:,.2f} from {org_name}"
    else:
        subject = f"Payment from {org_name} could not be processed"
    return await _send(to=to, subject=subject, html=html)


async def send_receipt_email(
    to: str,
    org_name: str,
    run_id_short: str,
    run_status: str,
    receipt_date: str,
    objective: str,
    candidates: list[dict],
    payout_total: float,
    platform_fee: float,
    total_deducted: float,
    fee_rate_pct: float,
    successful_count: int,
    fee_rule_label: str = "0.2% or ₦50 (whichever is higher)",
    approved_by: Optional[str] = None,
) -> bool:
    """Send a payment receipt to a recipient or vendor email address.

    Template: src/templates/emails/receipt.html
    candidates: list of {name, bank, account, amount, status}
    """
    html = _render(
        "receipt.html",
        org_name=org_name,
        run_id_short=run_id_short,
        run_status=run_status,
        receipt_date=receipt_date,
        objective=objective,
        candidates=candidates,
        payout_total=f"{payout_total:,.2f}",
        platform_fee=f"{platform_fee:,.2f}",
        total_deducted=f"{total_deducted:,.2f}",
        fee_rate_pct=f"{fee_rate_pct:.1f}",
        fee_rule_label=fee_rule_label,
        successful_count=successful_count,
        approved_by=approved_by,
    )
    return await _send(
        to=to,
        subject=f"Payment Receipt from {org_name} — ₦{payout_total:,.2f} disbursed",
        html=html,
    )


async def send_api_key_reveal_otp_email(
    to: str,
    display_name: str,
    key_name: str,
    code: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Send a 6-digit OTP to reveal a newly created API key."""
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "api_key_reveal_otp.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        key_name=key_name,
        code=code,
    )
    return await _send(
        to=to,
        subject=f"Your code to reveal API key \"{key_name}\" — FlowPilot",
        html=html,
    )


async def send_pin_reset_otp_email(
    to: str,
    display_name: str,
    code: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Send a 6-digit OTP to reset the approval PIN."""
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    html = _render(
        "verify_email.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        code=code,
    )
    return await _send(
        to=to,
        subject="Reset your FlowPilot approval PIN — verification code",
        html=html,
    )


def _parse_device(ua: Optional[str]) -> str:
    """Return a human-readable browser + OS label from a User-Agent string."""
    if not ua:
        return "Unknown device"
    u = ua.lower()
    # OS
    if "iphone" in u:
        os = "iPhone"
    elif "ipad" in u:
        os = "iPad"
    elif "android" in u:
        os = "Android"
    elif "windows" in u:
        os = "Windows"
    elif "macintosh" in u or "mac os x" in u:
        os = "macOS"
    elif "linux" in u:
        os = "Linux"
    else:
        os = "Unknown OS"
    # Browser (check Edge before Chrome; check Safari last since many UAs contain it)
    if "edg/" in u or "edge/" in u:
        browser = "Edge"
    elif "opr/" in u or "opera" in u:
        browser = "Opera"
    elif "chrome/" in u:
        browser = "Chrome"
    elif "firefox/" in u:
        browser = "Firefox"
    elif "safari/" in u:
        browser = "Safari"
    else:
        browser = "Unknown browser"
    return f"{browser} on {os}"


async def _get_location(ip: Optional[str]) -> str:
    """Resolve a human-readable location from an IP address via ip-api.com.

    Returns 'Local network' for private/loopback IPs; 'Unknown' on any failure.
    """
    if not ip:
        return "Unknown"
    # Private / loopback ranges — no external lookup needed
    if ip in ("127.0.0.1", "::1") or ip.startswith(("192.168.", "10.", "172.")):
        return "Local network"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,regionName,city"},
            )
        data = resp.json()
        if data.get("status") == "success":
            parts = [data.get("city"), data.get("regionName"), data.get("country")]
            return ", ".join(p for p in parts if p) or "Unknown"
    except Exception:
        pass
    return "Unknown"


async def send_login_notification_email(
    to: str,
    display_name: str,
    email: str,
    login_time: str,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify the user that a successful sign-in occurred on their account.

    Template: src/templates/emails/login_notification.html
    """
    base = frontend_url or Settings.FRONTEND_URL
    first_name = display_name.split()[0] if display_name else "there"
    device = _parse_device(user_agent)
    location = await _get_location(ip_address)
    html = _render(
        "login_notification.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        first_name=first_name,
        email=email,
        login_time=login_time,
        device=device,
        location=location,
        settings_url=f"{base}/dashboard/settings?tab=security",
        notifications_url=f"{base}/dashboard/settings?tab=notifications",
    )
    return await _send(
        to=to,
        subject="New sign-in to your FlowPilot account",
        html=html,
    )


async def send_account_deletion_code_email(
    to: str,
    display_name: str,
    code: str,
    frontend_url: Optional[str] = None,
) -> bool:
    """Send a 6-digit account deletion confirmation code."""
    base = frontend_url or Settings.FRONTEND_URL
    html = _render(
        "account_deletion_code.html",
        logo_url=f"{base}/brand/flowpilot_logo_darkblue.png",
        logo_dark_url=f"{base}/brand/flowpilot_logo.png",
        display_name=display_name,
        code=code,
        settings_url=f"{base}/dashboard/settings",
    )
    return await _send(
        to=to,
        subject="Your FlowPilot account deletion code",
        html=html,
    )
