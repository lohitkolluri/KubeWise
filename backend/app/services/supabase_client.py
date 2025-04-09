from datetime import datetime
from typing import Dict, List, Optional, Any, Union
from uuid import UUID

from loguru import logger
from supabase import create_client, Client
from realtime.connection import Socket

from app.core.config import settings
from app.models.database import (
    IssueCreate, IssueDB, IssueStatus, IssueSeverity,
    K8sObjectType, IssueUpdate, RemediationResult
)


class SupabaseService:
    """Service for interacting with Supabase."""

    def __init__(self):
        """Initialize Supabase client."""
        self.client: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
        self.realtime_channel = None
        self.tables_checked = False
        self.has_tables = False
        logger.info("Supabase client initialized")

    async def check_required_tables(self) -> bool:
        """
        Check if the required tables exist in Supabase.

        Returns:
            True if all tables exist, False otherwise
        """
        if self.tables_checked:
            return self.has_tables

        try:
            # Try to query the issues table
            _, count = self.client.table("issues").select("id").limit(1).execute()

            # If we get here, the table exists
            self.has_tables = True
            self.tables_checked = True
            logger.info("Supabase tables check: All required tables exist")
            return True
        except Exception as e:
            # If there's an error, the table likely doesn't exist
            self.has_tables = False
            self.tables_checked = True
            logger.warning(f"Supabase tables check: Required tables don't exist. Error: {e}")
            logger.warning("Please create the required tables using the create_supabase_tables.sql script")
            return False

    async def add_or_update_issue(self, issue: IssueCreate) -> IssueDB:
        """
        Add a new issue or update an existing one if similar issue exists.

        Args:
            issue: The issue to add or update

        Returns:
            The created or updated issue
        """
        try:
            # Check if similar issue exists
            data, count = self.client.table("issues").select("*").eq(
                "k8s_object_type", issue.k8s_object_type
            ).eq(
                "k8s_object_name", issue.k8s_object_name
            ).eq(
                "type", issue.type
            ).execute()

            # Get the current timestamp
            now = datetime.utcnow()

            # Handle different count return types
            issue_count = 0
            if isinstance(count, int):
                issue_count = count
            elif isinstance(count, tuple):
                if len(count) > 0:
                    if isinstance(count[0], int):
                        issue_count = count[0]
                    elif isinstance(count[0], str) and count[0] == 'count':
                        # Handle case where count is returned as ('count', value)
                        issue_count = count[1] if count[1] is not None else 0
            elif count is not None:
                logger.info(f"Count returned as {type(count)}: {count}")

            if issue_count > 0:
                # Issue exists, update it
                existing_issue = data[1][0]
                issue_id = existing_issue["id"]

                # Only update if not already resolved
                if existing_issue["status"] not in [IssueStatus.RESOLVED.value, IssueStatus.IGNORED.value]:
                    update_data = {
                        "last_detected_at": now.isoformat(),
                        "updated_at": now.isoformat(),
                        # Only update these if they changed
                        "severity": issue.severity,
                        "description": issue.description,
                        "remediation_suggestion": issue.remediation_suggestion,
                        "remediation_command": issue.remediation_command,
                        "ai_confidence_score": issue.ai_confidence_score
                    }

                    data, count = self.client.table("issues").update(
                        update_data
                    ).eq("id", issue_id).execute()

                    logger.info(f"Updated existing issue: {issue_id}")
                    return IssueDB(**data[1][0])
                else:
                    # Issue was already resolved or ignored, return it without updates
                    logger.info(f"Issue {issue_id} already {existing_issue['status']}, not updating")
                    return IssueDB(**existing_issue)
            else:
                # Create new issue
                data, count = self.client.table("issues").insert({
                    "type": issue.type,
                    "severity": issue.severity,
                    "k8s_object_type": issue.k8s_object_type,
                    "k8s_object_name": issue.k8s_object_name,
                    "k8s_object_namespace": issue.k8s_object_namespace,
                    "description": issue.description,
                    "remediation_suggestion": issue.remediation_suggestion,
                    "remediation_command": issue.remediation_command,
                    "ai_confidence_score": issue.ai_confidence_score,
                    "status": IssueStatus.NEW.value,
                    "last_detected_at": now.isoformat()
                }).execute()

                logger.info(f"Created new issue: {data[1][0]['id']}")
                return IssueDB(**data[1][0])
        except Exception as e:
            # If there's an error (likely due to missing table), log it and return a mock issue
            logger.error(f"Error in add_or_update_issue: {e}")

            # Create a mock issue object for development/testing
            mock_id = UUID("00000000-0000-0000-0000-000000000000")
            return IssueDB(
                id=mock_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                last_detected_at=datetime.utcnow(),
                type=issue.type,
                severity=issue.severity,
                k8s_object_type=issue.k8s_object_type,
                k8s_object_name=issue.k8s_object_name,
                k8s_object_namespace=issue.k8s_object_namespace,
                description=issue.description,
                status=IssueStatus.NEW,
                remediation_suggestion=issue.remediation_suggestion,
                remediation_command=issue.remediation_command,
                ai_confidence_score=issue.ai_confidence_score
            )

    async def get_issue_by_id(self, issue_id: UUID) -> Optional[IssueDB]:
        """
        Get an issue by its ID.

        Args:
            issue_id: The ID of the issue to get

        Returns:
            The issue if found, None otherwise
        """
        data, count = self.client.table("issues").select("*").eq("id", str(issue_id)).execute()

        if count > 0:
            return IssueDB(**data[1][0])
        return None

    async def list_issues(
        self,
        status: Optional[List[IssueStatus]] = None,
        severity: Optional[List[IssueSeverity]] = None,
        k8s_object_type: Optional[List[K8sObjectType]] = None,
        namespace: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[IssueDB]:
        """
        List issues with filtering.

        Args:
            status: Filter by issue status
            severity: Filter by severity
            k8s_object_type: Filter by Kubernetes object type
            namespace: Filter by namespace
            start_time: Filter by last detected time (start)
            end_time: Filter by last detected time (end)
            limit: Maximum number of issues to return
            offset: Offset for pagination

        Returns:
            List of issues matching the criteria
        """
        query = self.client.table("issues").select("*")

        # Apply filters
        if status:
            query = query.in_("status", [s.value for s in status])

        if severity:
            query = query.in_("severity", [s.value for s in severity])

        if k8s_object_type:
            query = query.in_("k8s_object_type", [t.value for t in k8s_object_type])

        if namespace:
            query = query.eq("k8s_object_namespace", namespace)

        if start_time:
            query = query.gte("last_detected_at", start_time.isoformat())

        if end_time:
            query = query.lte("last_detected_at", end_time.isoformat())

        # Apply pagination
        query = query.order("last_detected_at", desc=True).range(offset, offset + limit - 1)

        # Execute query
        data, count = query.execute()

        if count > 0:
            return [IssueDB(**item) for item in data[1]]
        return []

    async def update_issue_status(self, issue_id: UUID, status: IssueStatus) -> Optional[IssueDB]:
        """
        Update the status of an issue.

        Args:
            issue_id: The ID of the issue to update
            status: The new status

        Returns:
            The updated issue if found, None otherwise
        """
        now = datetime.utcnow()
        data, count = self.client.table("issues").update({
            "status": status.value,
            "updated_at": now.isoformat()
        }).eq("id", str(issue_id)).execute()

        if count > 0:
            logger.info(f"Updated issue {issue_id} status to {status.value}")
            return IssueDB(**data[1][0])
        return None

    async def update_issue(self, issue_id: UUID, update_data: IssueUpdate) -> Optional[IssueDB]:
        """
        Update issue fields.

        Args:
            issue_id: The ID of the issue to update
            update_data: The data to update

        Returns:
            The updated issue if found, None otherwise
        """
        # Convert to dict and filter out None values
        update_dict = update_data.dict(exclude_unset=True, exclude_none=True)

        if not update_dict:
            # Nothing to update
            return await self.get_issue_by_id(issue_id)

        # Add updated_at timestamp
        update_dict["updated_at"] = datetime.utcnow().isoformat()

        # Convert enum values to strings
        for key, value in update_dict.items():
            if hasattr(value, "value"):  # Check if it's an enum
                update_dict[key] = value.value

        data, count = self.client.table("issues").update(update_dict).eq("id", str(issue_id)).execute()

        if count > 0:
            logger.info(f"Updated issue {issue_id}")
            return IssueDB(**data[1][0])
        return None

    async def mark_stale_issues_as_resolved(self, time_threshold: datetime) -> int:
        """
        Mark issues that haven't been detected since time_threshold as resolved.

        Args:
            time_threshold: Issues last detected before this time will be marked as resolved

        Returns:
            Number of issues marked as resolved
        """
        try:
            # Convert datetime to ISO format string
            threshold_iso = time_threshold.isoformat()

            logger.info(f"Checking for issues last detected before {threshold_iso}")

            # First, check if there are any matching issues
            data, count = self.client.table("issues").select("id").lt("last_detected_at", threshold_iso).in_("status", [
                IssueStatus.NEW.value,
                IssueStatus.ACKNOWLEDGED.value,
                IssueStatus.REMEDIATION_ATTEMPTED.value
            ]).execute()

            # Handle different count return types
            issue_count = 0
            if isinstance(count, int):
                issue_count = count
            elif isinstance(count, tuple):
                if len(count) > 0:
                    if isinstance(count[0], int):
                        issue_count = count[0]
                    elif isinstance(count[0], str) and count[0] == 'count':
                        # Handle case where count is returned as ('count', value)
                        issue_count = count[1] if count[1] is not None else 0
            elif count is not None:
                logger.info(f"Count returned as {type(count)}: {count}")

            logger.info(f"Found {issue_count} stale issues to resolve")

            if issue_count == 0:
                logger.info("No stale issues found to resolve")
                return 0

            # Then update them
            update_data, update_count = self.client.table("issues").update({
                "status": IssueStatus.RESOLVED.value,
                "updated_at": datetime.utcnow().isoformat()
            }).lt("last_detected_at", threshold_iso).in_("status", [
                IssueStatus.NEW.value,
                IssueStatus.ACKNOWLEDGED.value,
                IssueStatus.REMEDIATION_ATTEMPTED.value
            ]).execute()

            # Handle different count return types for update operation
            resolved_count = 0
            if isinstance(update_count, int):
                resolved_count = update_count
            elif isinstance(update_count, tuple):
                if len(update_count) > 0:
                    if isinstance(update_count[0], int):
                        resolved_count = update_count[0]
                    elif isinstance(update_count[0], str) and update_count[0] == 'count':
                        # Handle case where count is returned as ('count', value)
                        resolved_count = update_count[1] if update_count[1] is not None else 0
            elif update_count is not None:
                logger.info(f"Update count returned as {type(update_count)}: {update_count}")

            if resolved_count > 0:
                logger.info(f"Marked {resolved_count} stale issues as resolved")
            return resolved_count
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            logger.error(f"Error marking stale issues as resolved: {str(e)}")
            logger.error(f"Error details: {error_details}")
            # Return 0 to indicate no issues were updated
            return 0

    async def log_remediation_result(self, remediation_result: RemediationResult) -> Dict[str, Any]:
        """
        Log a remediation result to the database.

        Args:
            remediation_result: The result of the remediation attempt

        Returns:
            The inserted record
        """
        # Convert to dict
        result_dict = remediation_result.dict()
        result_dict["issue_id"] = str(result_dict["issue_id"])  # Convert UUID to string
        result_dict["executed_at"] = result_dict["executed_at"].isoformat()  # Format datetime

        data, count = self.client.table("remediation_logs").insert(result_dict).execute()

        if count > 0:
            logger.info(f"Logged remediation result for issue {remediation_result.issue_id}")
            return data[1][0]
        return {}

    def subscribe_to_critical_issues(self, callback) -> None:
        """
        Subscribe to realtime updates for new critical issues.

        Args:
            callback: Function to call when a new critical issue is detected
        """
        logger.info("Realtime subscriptions not available in this version")
        # In supabase 1.0.3, the realtime subscription works differently
        # For now, we'll implement a polling mechanism in the main service
        pass

    def unsubscribe_realtime(self) -> None:
        """Unsubscribe from realtime updates."""
        # Nothing to do as we're not using realtime in this version
        pass


# Create a global instance for use across the application
supabase_service = SupabaseService()
