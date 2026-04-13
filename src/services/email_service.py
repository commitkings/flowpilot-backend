"""Email delivery via Resend API.

Templates live in src/templates/emails/ and are rendered with Jinja2.
All public send_* functions are fire-and-log — they return True/False but never raise.
"""

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


async def _send(to: str, subject: str, html: str) -> bool:
    """POST to Resend API.  Returns True on success, False on any failure."""
    api_key = Settings.get_resend_api_key()
    if not api_key:
        logger.warning("RESEND_API_KEY not configured — skipping email to %s", to)
        logger.info("Would send | subject=%r to=%s", subject, to)
        return False

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": Settings.DEFAULT_FROM_EMAIL,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                },
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
        first_name=first_name,
        code=code,
    )
    return await _send(
        to=to,
        subject="Your FlowPilot verification code",
        html=html,
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
        reset_url=reset_url,
    )
    return await _send(
        to=to,
        subject="Reset your FlowPilot password",
        html=html,
    )
