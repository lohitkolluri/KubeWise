import os
import re
import asyncio
import subprocess
from datetime import datetime
from typing import Dict, Any, Tuple, List, Optional
from uuid import UUID

from loguru import logger

from app.core.config import settings
from app.models.database import RemediationResult, IssueStatus
from app.services.supabase_client import supabase_service


class RemediationExecutor:
    """Service for executing kubectl commands as remediation for detected issues."""

    def __init__(self):
        """Initialize remediation service."""
        # Set up allowed kubectl verbs from settings
        self.allowed_kubectl_verbs = settings.ALLOWED_KUBECTL_VERBS

        # Regular expression to extract the main verb from kubectl command
        self.kubectl_verb_regex = re.compile(r'kubectl\s+([a-z]+)')

        # Additional safety patterns (commands that should never be allowed)
        self.forbidden_patterns = [
            r'delete\s+(namespace|ns)',  # Deleting namespaces is too dangerous
            r'delete\s+(-f|--filename)',  # Deleting from files
            r'delete\s+(deployments|deploy|services|svc|statefulsets|sts|daemonsets|ds|configmaps|cm|secrets)\s+--all',  # Bulk deletion
            r'apply\s+(-f|--filename)',  # Apply from files
            r'create\s+(-f|--filename)',  # Create from files
            r'exec\s+.*(\||>)',  # Commands with redirection/piping
            r'exec\s+.*--(command|c)\s*=.*\s*rm\s',  # Exec commands with rm
            r'port-forward',  # Port forwarding
            r'proxy',  # Running proxy
            r'.*--all-namespaces',  # Operations on all namespaces
            r'scale\s+.*replicas=0',  # Scaling to zero
            r'cordon|drain|uncordon',  # Node management
            r'.*[\'\"\`].*\$\(.*[\'\"\`]',  # Shell command substitution
            r'.*\|\s*sh',  # Piping to shell
            r'.*\&\&\s*[\w]+',  # Command chaining
        ]

    def validate_command(self, command: str) -> Tuple[bool, str]:
        """
        Validate if the kubectl command is allowed to run.

        Args:
            command: The kubectl command to validate

        Returns:
            Tuple of (is_valid, reason)
        """
        # Check if command starts with kubectl
        if not command.strip().startswith('kubectl'):
            return False, "Only kubectl commands are allowed"

        # Extract the main verb from command
        match = self.kubectl_verb_regex.search(command)
        if not match:
            return False, "Could not extract kubectl verb from command"

        verb = match.group(1)

        # Check if verb is in allowed list
        if verb not in self.allowed_kubectl_verbs:
            return False, f"Kubectl verb '{verb}' is not allowed. Allowed verbs: {', '.join(self.allowed_kubectl_verbs)}"

        # Check for forbidden patterns
        for pattern in self.forbidden_patterns:
            if re.search(pattern, command):
                return False, f"Command contains forbidden pattern: {pattern}"

        return True, "Command is valid"

    async def execute_remediation(self, issue_id: UUID, command: Optional[str] = None) -> RemediationResult:
        """
        Execute a kubectl command as remediation for an issue.

        Args:
            issue_id: ID of the issue to remediate
            command: Optional override command. If not provided, the command from the issue will be used.

        Returns:
            Result of the remediation
        """
        # Get issue from database
        issue = await supabase_service.get_issue_by_id(issue_id)
        if not issue:
            error_msg = f"Issue {issue_id} not found"
            logger.error(error_msg)
            return RemediationResult(
                issue_id=issue_id,
                command="",
                stdout="",
                stderr=error_msg,
                return_code=1,
                executed_at=datetime.utcnow(),
                success=False
            )

        # Get command to execute
        remediation_command = command or issue.remediation_command
        if not remediation_command:
            error_msg = f"No remediation command available for issue {issue_id}"
            logger.error(error_msg)
            return RemediationResult(
                issue_id=issue_id,
                command="",
                stdout="",
                stderr=error_msg,
                return_code=1,
                executed_at=datetime.utcnow(),
                success=False
            )

        # Validate command
        is_valid, reason = self.validate_command(remediation_command)
        if not is_valid:
            error_msg = f"Command validation failed: {reason}"
            logger.error(f"{error_msg} - Command: {remediation_command}")
            return RemediationResult(
                issue_id=issue_id,
                command=remediation_command,
                stdout="",
                stderr=error_msg,
                return_code=1,
                executed_at=datetime.utcnow(),
                success=False
            )

        try:
            # Split command into args
            cmd_args = remediation_command.split()

            # Prepare environment with KUBECONFIG
            env = os.environ.copy()
            env['KUBECONFIG'] = settings.KUBECONFIG_PATH

            # Execute command
            logger.info(f"Executing remediation command: {remediation_command}")
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )

            # Wait for command to complete
            stdout, stderr = await process.communicate()

            # Convert bytes to strings
            stdout_str = stdout.decode('utf-8')
            stderr_str = stderr.decode('utf-8')
            return_code = process.returncode

            # Determine success
            success = return_code == 0

            # Create result
            result = RemediationResult(
                issue_id=issue_id,
                command=remediation_command,
                stdout=stdout_str,
                stderr=stderr_str,
                return_code=return_code,
                executed_at=datetime.utcnow(),
                success=success
            )

            # Log result to database
            await supabase_service.log_remediation_result(result)

            # Update issue status
            await supabase_service.update_issue_status(
                issue_id=issue_id,
                status=IssueStatus.REMEDIATION_ATTEMPTED
            )

            logger.info(f"Remediation command executed with return code {return_code}")
            return result

        except Exception as e:
            error_msg = f"Error executing remediation command: {str(e)}"
            logger.error(error_msg)
            result = RemediationResult(
                issue_id=issue_id,
                command=remediation_command,
                stdout="",
                stderr=error_msg,
                return_code=1,
                executed_at=datetime.utcnow(),
                success=False
            )

            # Log result to database even if it failed
            await supabase_service.log_remediation_result(result)

            return result


# Create a global instance for use across the application
remediation_executor = RemediationExecutor()
