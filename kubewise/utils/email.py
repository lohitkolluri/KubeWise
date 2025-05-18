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
            # Identify ourselves to the SMTP server
            server.ehlo()
            
            # Enable TLS encryption
            server.starttls()
            
            # Re-identify ourselves over the secure connection
            server.ehlo()
            
            # Login if password is provided
            if settings.smtp_password:
                server.login(settings.smtp_sender_email, settings.smtp_password.get_secret_value())
                
            # Send the email
            server.sendmail(settings.smtp_sender_email, to_email, msg.as_string())
        
        logger.info(f"Email sent successfully to {to_email}")

    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"SMTP authentication failed for {settings.smtp_sender_email}: {e}")
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error occurred while sending email to {to_email}: {e}")
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")