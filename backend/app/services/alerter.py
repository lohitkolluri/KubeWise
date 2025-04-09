import os
import smtplib
import asyncio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Dict, Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from app.core.config import settings
from app.models.database import IssueDB, IssueSeverity


class AlertService:
    """Service for sending alerts about critical issues."""

    def __init__(self):
        """Initialize the alerting service."""
        # Set up Jinja2 environment
        templates_dir = Path(__file__).parent.parent / "templates"
        self.jinja_env = Environment(
            loader=FileSystemLoader(templates_dir),
            autoescape=select_autoescape(['html'])
        )

        # Load the email template
        try:
            self.template = self.jinja_env.get_template("alert.html")
            logger.info("Email template loaded successfully")
        except Exception as e:
            self.template = None
            logger.error(f"Failed to load email template: {e}")

        # Initialize SMTP settings from config
        self.smtp_settings = {
            'smtp_server': settings.SMTP_HOST,
            'smtp_port': settings.SMTP_PORT,
            'smtp_use_tls': settings.SMTP_USE_TLS,
            'smtp_use_ssl': settings.SMTP_USE_SSL,
            'smtp_username': settings.SMTP_USER,
            'smtp_password': settings.SMTP_PASSWORD,
            'smtp_from_email': settings.ALERT_SENDER_EMAIL,
            'smtp_to_email': settings.ALERT_RECIPIENT_EMAIL
        }

        # Log SMTP configuration (without password)
        logger.info(f"SMTP Configuration: Server={self.smtp_settings['smtp_server']}, "
                   f"Port={self.smtp_settings['smtp_port']}, "
                   f"TLS={self.smtp_settings['smtp_use_tls']}, "
                   f"SSL={self.smtp_settings['smtp_use_ssl']}, "
                   f"Username={self.smtp_settings['smtp_username']}, "
                   f"From={self.smtp_settings['smtp_from_email']}, "
                   f"To={self.smtp_settings['smtp_to_email']}")

    def _get_zoho_smtp_server(self, email: str) -> str:
        """
        Determine the correct Zoho SMTP server based on email domain.

        Args:
            email: The email address to check

        Returns:
            The appropriate SMTP server for the email domain
        """
        if '@zoho.in' in email:
            return 'smtp.zoho.in'
        elif '@zoho.com' in email:
            return 'smtp.zoho.com'
        elif '@zoho.eu' in email:
            return 'smtp.zoho.eu'
        elif '@zohomail.com' in email:
            return 'smtp.zoho.com'  # zohomail.com accounts use smtp.zoho.com
        else:
            # For custom domains with Zoho Workplace
            return 'smtp.zoho.in'  # Use smtp.zoho.in for all custom domains

    async def send_alert(self, issue: IssueDB) -> bool:
        """
        Send an alert for a critical issue.

        Args:
            issue: The issue to alert about

        Returns:
            bool: True if alert was sent successfully, False otherwise
        """
        if not self.smtp_settings:
            logger.warning("SMTP settings not configured, skipping alert")
            return False

        max_retries = 3
        retry_delay = 1  # seconds
        smtp_timeout = 30  # seconds

        for attempt in range(max_retries):
            try:
                # Create message
                msg = MIMEMultipart()
                msg['From'] = self.smtp_settings['smtp_from_email']
                msg['To'] = self.smtp_settings['smtp_to_email']
                msg['Subject'] = f"Kubernetes Alert: {issue.type} - {issue.severity}"

                # Format email body
                body = self.template.render(issue=issue)
                msg.attach(MIMEText(body, 'html'))

                # Determine the correct SMTP server for Zoho Mail
                smtp_server = self.smtp_settings['smtp_server']
                if 'zoho' in smtp_server.lower() or 'zoho' in self.smtp_settings['smtp_from_email'].lower():
                    smtp_server = self._get_zoho_smtp_server(self.smtp_settings['smtp_from_email'])
                    logger.info(f"Using Zoho SMTP server: {smtp_server}")

                # Connect to SMTP server with timeout
                logger.info(f"Connecting to SMTP server {smtp_server}:{self.smtp_settings['smtp_port']}")

                # Determine which connection type to use based on settings
                if self.smtp_settings['smtp_use_ssl']:
                    # SSL connection for port 465
                    logger.info("Using SSL connection for SMTP")
                    server = smtplib.SMTP_SSL(
                        smtp_server,
                        self.smtp_settings['smtp_port'],
                        timeout=smtp_timeout
                    )
                    # Enable debug mode for troubleshooting
                    server.set_debuglevel(1)

                    # Login with credentials
                    if self.smtp_settings['smtp_username'] and self.smtp_settings['smtp_password']:
                        username = self.smtp_settings['smtp_username']
                        # Ensure username is a full email address
                        if '@' not in username:
                            # Extract domain from from_email if possible
                            if '@' in self.smtp_settings['smtp_from_email']:
                                domain = self.smtp_settings['smtp_from_email'].split('@')[1]
                                username = f"{username}@{domain}"
                            else:
                                # Fallback to constructing from SMTP server
                                domain = smtp_server.split('.')[-2] + '.' + smtp_server.split('.')[-1]
                                username = f"{username}@{domain}"

                        logger.info(f"Attempting SSL login with username: {username}")
                        server.login(
                            username,
                            self.smtp_settings['smtp_password']
                        )

                    # Send email
                    server.send_message(msg)
                    logger.info(f"Alert sent successfully using SSL to {self.smtp_settings['smtp_to_email']}")
                    return True

                elif self.smtp_settings['smtp_use_tls']:
                    # TLS connection (typical for port 587)
                    try:
                        server = smtplib.SMTP(
                            smtp_server,
                            self.smtp_settings['smtp_port'],
                            timeout=smtp_timeout
                        )

                        # Enable debug mode for troubleshooting
                        server.set_debuglevel(1)

                        # Start TLS
                        server.ehlo()  # Required for TLS
                        server.starttls()
                        server.ehlo()  # Required after TLS

                        # Login with credentials
                        if self.smtp_settings['smtp_username'] and self.smtp_settings['smtp_password']:
                            # Use the full email address as username
                            username = self.smtp_settings['smtp_username']

                            # Ensure username is a full email address
                            if '@' not in username:
                                # Extract domain from from_email if possible
                                if '@' in self.smtp_settings['smtp_from_email']:
                                    domain = self.smtp_settings['smtp_from_email'].split('@')[1]
                                    username = f"{username}@{domain}"
                                else:
                                    # Fallback to constructing from SMTP server
                                    domain = smtp_server.split('.')[-2] + '.' + smtp_server.split('.')[-1]
                                    username = f"{username}@{domain}"

                            logger.info(f"Attempting SMTP login with username: {username}")
                            server.login(
                                username,
                                self.smtp_settings['smtp_password']
                            )

                        # Send email
                        server.send_message(msg)
                        logger.info(f"Alert sent successfully to {self.smtp_settings['smtp_to_email']}")
                        return True

                    except smtplib.SMTPAuthenticationError as e:
                        logger.error(f"SMTP authentication failed: {e}")
                        logger.info("Trying SSL connection as fallback...")

                        # Try SSL as fallback (port 465)
                        try:
                            server = smtplib.SMTP_SSL(
                                smtp_server,
                                465,  # SSL port
                                timeout=smtp_timeout
                            )

                            # Enable debug mode for troubleshooting
                            server.set_debuglevel(1)

                            # Login with credentials
                            if self.smtp_settings['smtp_username'] and self.smtp_settings['smtp_password']:
                                # Use the full email address as username
                                username = self.smtp_settings['smtp_username']

                                # Ensure username is a full email address
                                if '@' not in username:
                                    # Extract domain from from_email if possible
                                    if '@' in self.smtp_settings['smtp_from_email']:
                                        domain = self.smtp_settings['smtp_from_email'].split('@')[1]
                                        username = f"{username}@{domain}"
                                    else:
                                        # Fallback to constructing from SMTP server
                                        domain = smtp_server.split('.')[-2] + '.' + smtp_server.split('.')[-1]
                                        username = f"{username}@{domain}"

                                logger.info(f"Attempting SSL login with username: {username}")
                                server.login(
                                    username,
                                    self.smtp_settings['smtp_password']
                                )

                            # Send email
                            server.send_message(msg)
                            logger.info(f"Alert sent successfully using SSL to {self.smtp_settings['smtp_to_email']}")
                            return True

                        except Exception as ssl_e:
                            logger.error(f"SSL authentication also failed: {ssl_e}")
                            if attempt < max_retries - 1:
                                # Exponential backoff for retries
                                wait_time = retry_delay * (2 ** attempt)
                                logger.info(f"Retrying in {wait_time} seconds...")
                                await asyncio.sleep(wait_time)
                            else:
                                logger.error("All retry attempts failed to send alert")
                                return False

                    except Exception as e:
                        logger.error(f"SMTP error occurred: {e}")
                        if attempt < max_retries - 1:
                            # Exponential backoff for retries
                            wait_time = retry_delay * (2 ** attempt)
                            logger.info(f"Retrying in {wait_time} seconds...")
                            await asyncio.sleep(wait_time)
                        else:
                            logger.error("All retry attempts failed to send alert")
                            return False
                    finally:
                        try:
                            server.quit()
                        except Exception as e:
                            logger.warning(f"Error closing SMTP connection: {e}")
                else:
                    # Non-TLS connection (not recommended)
                    logger.warning("Using non-TLS SMTP connection (not recommended)")
                    logger.warning("For port 465, please use SMTP_USE_SSL=True instead of plain connection")
                    server = smtplib.SMTP(
                        smtp_server,
                        self.smtp_settings['smtp_port'],
                        timeout=smtp_timeout
                    )

                    # Enable debug mode for troubleshooting
                    server.set_debuglevel(1)

                    # Login with credentials
                    if self.smtp_settings['smtp_username'] and self.smtp_settings['smtp_password']:
                        server.login(
                            self.smtp_settings['smtp_username'],
                            self.smtp_settings['smtp_password']
                        )

                    # Send email
                    server.send_message(msg)
                    logger.info(f"Alert sent successfully to {self.smtp_settings['smtp_to_email']}")
                    return True

            except Exception as e:
                logger.error(f"Unexpected error sending alert: {e}")
                if attempt < max_retries - 1:
                    # Exponential backoff for retries
                    wait_time = retry_delay * (2 ** attempt)
                    logger.info(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("All retry attempts failed to send alert")
                    return False

        return False

    async def process_new_critical_issue(self, issue: IssueDB) -> None:
        """
        Process a new critical issue and send alert if needed.

        Args:
            issue: The new issue to process
        """
        if issue.severity == IssueSeverity.CRITICAL:
            await self.send_alert(issue)

    def handle_supabase_notification(self, payload: Dict[str, Any]) -> None:
        """
        Handle a notification from Supabase realtime.

        Args:
            payload: The notification payload
        """
        try:
            # Extract the new issue data
            new_record = payload.get('new', {})

            # Check if it's a critical issue
            if new_record.get('severity') == IssueSeverity.CRITICAL.value:
                # Convert to IssueDB model
                issue = IssueDB(**new_record)

                # Send alert (use asyncio.create_task in a non-async context)
                asyncio.create_task(self.send_alert(issue))

                logger.info(f"Processing realtime notification for critical issue: {issue.id}")
        except Exception as e:
            logger.error(f"Error processing Supabase notification: {e}")


# Create a global instance for use across the application
alert_service = AlertService()
