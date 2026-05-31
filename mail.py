"""
Email sending via SMTP.

Config keys (all optional — emails silently skipped if not configured):
  SMTP_HOST      e.g. "smtp.gmail.com"
  SMTP_PORT      e.g. 587
  SMTP_USERNAME  e.g. "automations@sbo-ovsar.ca"
  SMTP_PASSWORD  Gmail app password or SMTP password
  SMTP_FROM      display name + address, e.g. "SBO-OVSAR Hours Log <automations@sbo-ovsar.ca>"
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

_cfg: dict = {}
_dry_run: bool = False


def init_mail(config: dict) -> None:
    global _cfg, _dry_run
    _cfg = {k: config.get(k) for k in
            ('SMTP_HOST', 'SMTP_PORT', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM')}
    _dry_run = bool(config.get('MAIL_DRY_RUN'))
    if _dry_run:
        logger.info('Mail: DRY RUN mode — emails will be logged but not sent')
    elif _cfg.get('SMTP_HOST'):
        logger.info(f"Mail: configured via {_cfg['SMTP_HOST']}:{_cfg.get('SMTP_PORT', 587)}")
    else:
        logger.info('Mail: not configured — notifications disabled')


def _is_configured() -> bool:
    return _dry_run or bool(_cfg.get('SMTP_HOST') and _cfg.get('SMTP_USERNAME') and _cfg.get('SMTP_PASSWORD'))


def send(to: str, subject: str, body_html: str, body_text: str = '') -> bool:
    if not _is_configured():
        logger.debug(f'Mail not configured — skipping email to {to}: {subject}')
        return False
    if _dry_run:
        logger.info(f'Mail sent to {to}: {subject}')
        return True
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = _cfg.get('SMTP_FROM') or _cfg['SMTP_USERNAME']
        msg['To'] = to
        if body_text:
            msg.attach(MIMEText(body_text, 'plain'))
        msg.attach(MIMEText(body_html, 'html'))

        port = int(_cfg.get('SMTP_PORT') or 587)
        with smtplib.SMTP(_cfg['SMTP_HOST'], port, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.login(_cfg['SMTP_USERNAME'], _cfg['SMTP_PASSWORD'])
            s.sendmail(msg['From'], [to], msg.as_string())
        logger.info(f'Mail sent to {to}: {subject}')
        return True
    except Exception as e:
        logger.error(f'Mail failed to {to}: {e}')
        return False


# ── Notification helpers ──────────────────────────────────────────────────────

def notify_record_approved(user_email: str, user_name: str, record) -> None:
    subject = 'Your hours record was approved'
    html = f"""
<p>Hi {user_name},</p>
<p>Your hours record has been <strong>approved</strong>.</p>
<ul>
  <li><strong>Date:</strong> {record.date}</li>
  <li><strong>Hours:</strong> {record.hours}</li>
  <li><strong>Category:</strong> {record.category.name if record.category else '—'}</li>
</ul>
<p><a href="https://hours.sbo-ovsar.ca/hours">View your records</a></p>
<p style="color:#999;font-size:0.85em">SBO-OVSAR Hours Log · <a href="https://hours.sbo-ovsar.ca/profile">manage notifications</a></p>
"""
    send(user_email, subject, html)


def notify_record_rejected(user_email: str, user_name: str, record, reason: str = '') -> None:
    subject = 'Your hours record was not approved'
    html = f"""
<p>Hi {user_name},</p>
<p>Your hours record was <strong>not approved</strong>.</p>
<ul>
  <li><strong>Date:</strong> {record.date}</li>
  <li><strong>Hours:</strong> {record.hours}</li>
  <li><strong>Category:</strong> {record.category.name if record.category else '—'}</li>
  {'<li><strong>Reason:</strong> ' + reason + '</li>' if reason else ''}
</ul>
<p><a href="https://hours.sbo-ovsar.ca/hours">View your records</a></p>
<p style="color:#999;font-size:0.85em">SBO-OVSAR Hours Log · <a href="https://hours.sbo-ovsar.ca/profile">manage notifications</a></p>
"""
    send(user_email, subject, html)


def notify_pending_submitted(approver_email: str, approver_name: str,
                              submitter_name: str, record,
                              member_names: list = None) -> None:
    for_line = ', '.join(member_names) if member_names else submitter_name
    subject = f'New hours record pending approval — {for_line}'
    html = f"""
<p>Hi {approver_name},</p>
<p><strong>{submitter_name}</strong> has submitted a hours record for approval.</p>
<ul>
  <li><strong>For:</strong> {for_line}</li>
  <li><strong>Date:</strong> {record.date}</li>
  <li><strong>Hours:</strong> {record.hours}</li>
  <li><strong>Category:</strong> {record.category.name if record.category else '—'}</li>
  {'<li><strong>Description:</strong> ' + record.description + '</li>' if record.description else ''}
</ul>
<p><a href="https://hours.sbo-ovsar.ca/approvals">Review pending records</a></p>
<p style="color:#999;font-size:0.85em">SBO-OVSAR Hours Log · <a href="https://hours.sbo-ovsar.ca/profile">manage notifications</a></p>
"""
    send(approver_email, subject, html)


def send_weekly_summary(user_email: str, user_name: str,
                        pending: int, approved_week: int, tc_hrs: float) -> None:
    subject = 'Your hours log summary'
    html = f"""
<p>Hi {user_name},</p>
<p>Here's your hours log summary:</p>
<ul>
  <li><strong>Tax credit hours:</strong> {tc_hrs:.1f} / 200</li>
  <li><strong>Records pending approval:</strong> {pending}</li>
  <li><strong>Records approved recently:</strong> {approved_week}</li>
</ul>
<p><a href="https://hours.sbo-ovsar.ca/profile">View your profile</a></p>
<p style="color:#999;font-size:0.85em">SBO-OVSAR Hours Log · <a href="https://hours.sbo-ovsar.ca/profile">manage notifications</a></p>
"""
    send(user_email, subject, html)


def send_monthly_progress(user_email: str, user_name: str, month_name: str,
                          tc_hrs: float, primary_hrs: float, secondary_hrs: float,
                          other_hrs: float, total_hrs: float) -> None:
    pct = min(100, round(tc_hrs / 200 * 100))
    subject = f'Your hours progress — {month_name}'
    html = f"""
<p>Hi {user_name},</p>
<p>Here's your hours progress for <strong>{month_name}</strong>:</p>
<table style="border-collapse:collapse;width:100%;max-width:400px;font-size:0.9em">
  <tr><td style="padding:4px 0;color:#555">Primary</td><td style="text-align:right;font-weight:700">{primary_hrs:.1f} hrs</td></tr>
  <tr><td style="padding:4px 0;color:#555">Secondary</td><td style="text-align:right;font-weight:700">{secondary_hrs:.1f} hrs</td></tr>
  <tr><td style="padding:4px 0;color:#555">Other</td><td style="text-align:right;font-weight:700">{other_hrs:.1f} hrs</td></tr>
  <tr style="border-top:1px solid #eee"><td style="padding:6px 0;font-weight:700">Tax Credit Total</td><td style="text-align:right;font-weight:700;color:#1a73e8">{tc_hrs:.1f} / 200 hrs ({pct}%)</td></tr>
</table>
<p><a href="https://hours.sbo-ovsar.ca/profile">View your full profile</a></p>
<p style="color:#999;font-size:0.85em">SBO-OVSAR Hours Log · <a href="https://hours.sbo-ovsar.ca/profile">manage notifications</a></p>
"""
    send(user_email, subject, html)


def send_tax_credit_eligible(user_email: str, user_name: str,
                              tc_hrs: float, year: int) -> None:
    subject = f'You\'ve reached tax credit eligibility — {year}'
    html = f"""
<p>Hi {user_name},</p>
<p>🎉 You've reached <strong>tax credit eligibility</strong> for {year}!</p>
<p>Your tax credit hours are <strong>{tc_hrs:.1f} / 200</strong> and your primary hours exceed secondary — both conditions met.</p>
<p><a href="https://hours.sbo-ovsar.ca/profile">View your profile</a></p>
<p style="color:#999;font-size:0.85em">SBO-OVSAR Hours Log · <a href="https://hours.sbo-ovsar.ca/profile">manage notifications</a></p>
"""
    send(user_email, subject, html)
