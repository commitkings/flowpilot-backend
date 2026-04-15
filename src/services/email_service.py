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
        dashboard_url=f"{base}/dashboard",
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
    frontend_url: Optional[str] = None,
) -> bool:
    """Notify the business owner that their KYC has been approved.

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
        dashboard_url=f"{base}/dashboard",
    )
    return await _send(
        to=to,
        subject=f"{business_name} is verified on FlowPilot — you're ready to go!",
        html=html,
    )


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
    )
    return await _send(
        to=to,
        subject=f"Reminder: \"{schedule_name}\" runs tomorrow",
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
    )
    return await _send(
        to=to,
        subject=f"₦{amount:,.2f} added to your FlowPilot wallet",
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
    )
    return await _send(
        to=to,
        subject="Low wallet balance — top up to keep your runs running",
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
