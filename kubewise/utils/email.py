import smtplib
from email.mime.text import MIMEText
from loguru import logger
from kubewise.config import settings

def send_email(to_email: str, subject: str, body: str):
    """
    Sends an email using the configured SMTP settings.

    Args:
        to_email: The recipient's email address.
        subject: The subject of the email.
        body: The body content of the email.
    """
    if not settings.smtp_server or not settings.smtp_sender_email:
        logger.warning("SMTP server or sender email not configured. Skipping email sending.")
        return

    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = settings.smtp_sender_email
        msg['To'] = to_email

        with smtplib.SMTP(settings.smtp_server, settings.smtp_port) as server:
            if settings.smtp_password:
                server.login(settings.smtp_sender_email, settings.smtp_password.get_secret_value())
            server.sendmail(settings.smtp_sender_email, to_email, msg.as_string())
        logger.info(f"Email sent successfully to {to_email}")

    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")