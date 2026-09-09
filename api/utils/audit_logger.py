"""
Audit logging service.

Provides functions to log security-sensitive operations to the audit log.
"""

import secrets
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from api.models import AuditAction, AuditLog


def log_audit_event(
    db: Session,
    action: str,
    user_id: str | None = None,
    user_email: str | None = None,
    organization_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    status: str = "success",
    error_message: str | None = None,
    commit: bool = True,
) -> AuditLog:
    """
    Log an audit event to the database.

    Args:
        db: Database session
        action: Action performed (use AuditAction constants)
        user_id: ID of user who performed action
        user_email: Email of user (denormalized for easy lookup)
        organization_id: Organization context
        resource_type: Type of resource affected (e.g., "person", "event")
        resource_id: ID of affected resource
        details: Additional context (before/after values, etc.)
        ip_address: Client IP address
        user_agent: Browser/client user agent string
        status: "success", "failure", or "denied"
        error_message: Error message if status = "failure"
        commit: Commit immediately; set False to join the caller's transaction.

    Returns:
        Created AuditLog instance
    """
    audit_log = AuditLog(
        id=f"audit_{secrets.token_urlsafe(16)}",
        user_id=user_id,
        user_email=user_email,
        organization_id=organization_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
        ip_address=ip_address,
        user_agent=user_agent,
        status=status,
        error_message=error_message,
    )

    db.add(audit_log)
    if commit:
        db.commit()
        db.refresh(audit_log)

    return audit_log


def log_audit_from_request(
    db: Session,
    request: Request,
    action: str,
    user_id: str | None = None,
    user_email: str | None = None,
    organization_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    details: dict[str, Any] | None = None,
    status: str = "success",
    error_message: str | None = None,
) -> AuditLog:
    """
    Log an audit event, extracting IP and user agent from FastAPI Request.

    This is a convenience wrapper around log_audit_event that automatically
    extracts the client IP address and user agent from the request.

    Args:
        db: Database session
        request: FastAPI Request object
        action: Action performed (use AuditAction constants)
        (other args same as log_audit_event)

    Returns:
        Created AuditLog instance
    """
    # Extract IP address (handle proxy headers)
    ip_address = request.client.host if request.client else None
    if "x-forwarded-for" in request.headers:
        # Use first IP in X-Forwarded-For (client IP)
        ip_address = request.headers["x-forwarded-for"].split(",")[0].strip()
    elif "x-real-ip" in request.headers:
        ip_address = request.headers["x-real-ip"]

    # Extract user agent
    user_agent = request.headers.get("user-agent")

    return log_audit_event(
        db=db,
        action=action,
        user_id=user_id,
        user_email=user_email,
        organization_id=organization_id,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        ip_address=ip_address,
        user_agent=user_agent,
        status=status,
        error_message=error_message,
    )


def log_login_attempt(
    db: Session,
    request: Request,
    email: str,
    success: bool,
    user_id: str | None = None,
    organization_id: str | None = None,
    error_message: str | None = None,
) -> AuditLog:
    """
    Log a login attempt (success or failure).

    Args:
        db: Database session
        request: FastAPI Request object
        email: Email used for login attempt
        success: Whether login succeeded
        user_id: User ID (if login succeeded)
        organization_id: Organization ID (if login succeeded)
        error_message: Error message if login failed

    Returns:
        Created AuditLog instance
    """
    action = AuditAction.LOGIN_SUCCESS if success else AuditAction.LOGIN_FAILURE
    status = "success" if success else "failure"

    return log_audit_from_request(
        db=db,
        request=request,
        action=action,
        user_id=user_id,
        user_email=email,
        organization_id=organization_id,
        status=status,
        error_message=error_message,
    )


def log_permission_change(
    db: Session,
    request: Request,
    admin_user_id: str,
    admin_email: str,
    target_user_id: str,
    target_email: str,
    organization_id: str,
    old_roles: list[str],
    new_roles: list[str],
) -> AuditLog:
    """
    Log a permission/role change.

    Args:
        db: Database session
        request: FastAPI Request object
        admin_user_id: ID of admin making the change
        admin_email: Email of admin making the change
        target_user_id: ID of user being modified
        target_email: Email of user being modified
        organization_id: Organization context
        old_roles: Previous roles
        new_roles: New roles

    Returns:
        Created AuditLog instance
    """
    added_roles = set(new_roles) - set(old_roles)
    removed_roles = set(old_roles) - set(new_roles)

    details = {
        "target_user_id": target_user_id,
        "target_email": target_email,
        "old_roles": old_roles,
        "new_roles": new_roles,
        "added_roles": list(added_roles),
        "removed_roles": list(removed_roles),
    }

    if added_roles:
        action = AuditAction.ROLE_ASSIGNED
    elif removed_roles:
        action = AuditAction.ROLE_REMOVED
    else:
        # No actual change (shouldn't happen, but handle gracefully)
        action = "permission.role.unchanged"

    return log_audit_from_request(
        db=db,
        request=request,
        action=action,
        user_id=admin_user_id,
        user_email=admin_email,
        organization_id=organization_id,
        resource_type="person",
        resource_id=target_user_id,
        details=details,
    )


def log_data_export(
    db: Session,
    request: Request,
    user_id: str,
    user_email: str,
    organization_id: str,
    export_type: str,  # "calendar", "report", "csv"
    resource_type: str | None = None,
    resource_count: int | None = None,
) -> AuditLog:
    """
    Log a data export operation.

    Args:
        db: Database session
        request: FastAPI Request object
        user_id: ID of user exporting data
        user_email: Email of user exporting data
        organization_id: Organization context
        export_type: Type of export ("calendar", "report", "csv")
        resource_type: Type of resource exported (e.g., "events", "people")
        resource_count: Number of records exported

    Returns:
        Created AuditLog instance
    """
    action = (
        AuditAction.CALENDAR_EXPORTED if export_type == "calendar" else AuditAction.DATA_EXPORTED
    )

    details = {
        "export_type": export_type,
        "resource_type": resource_type,
        "resource_count": resource_count,
    }

    return log_audit_from_request(
        db=db,
        request=request,
        action=action,
        user_id=user_id,
        user_email=user_email,
        organization_id=organization_id,
        details=details,
    )
