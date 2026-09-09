"""Organization router."""

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.database import get_db
from api.dependencies import (
    check_admin_permission,
    get_current_admin_user,
    get_current_user,
    verify_org_member,
)
from api.models import AuditAction, Organization, Person
from api.schemas.common import PaginationParams, get_pagination_params
from api.schemas.organization import (
    OrganizationCreate,
    OrganizationList,
    OrganizationResponse,
    OrganizationUpdate,
)
from api.timeutils import utcnow
from api.utils.audit_logger import log_audit_event
from api.utils.rate_limit_middleware import rate_limit

router = APIRouter(prefix="/organizations", tags=["organizations"])


def _get_scoped_organization(
    org_id: str, db: Session, actor: Person, *, admin: bool = False
) -> Organization:
    if admin and not check_admin_permission(actor):
        raise HTTPException(status_code=403, detail="Admin access required")
    verify_org_member(actor, org_id)
    org = db.scalar(select(Organization).where(Organization.id == actor.org_id))
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


def _commit_organization_change(
    db: Session,
    org_id: str,
    actor: Person,
    action: str,
    request: Request | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Keep the lifecycle mutation and its audit evidence in one transaction."""
    try:
        log_audit_event(
            db,
            action=action,
            user_id=actor.id,
            user_email=actor.email,
            organization_id=org_id,
            resource_type="organization",
            resource_id=org_id,
            details=details,
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
            commit=False,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise


@router.post(
    "/",
    response_model=OrganizationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("create_org"))],
)
def create_organization(org_data: OrganizationCreate, db: Session = Depends(get_db)):
    """Public onboarding exception: create a new, empty organization.

    Rate limited to 2 requests per hour per IP. Reading or changing an
    existing organization requires authenticated membership.
    """
    # Check if organization already exists
    existing = db.query(Organization).filter(Organization.id == org_data.id).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Organization with ID '{org_data.id}' already exists",
        )

    # Create organization
    org = Organization(
        id=org_data.id,
        name=org_data.name,
        region=org_data.region,
        config=org_data.config or {},
    )
    db.add(org)
    db.commit()
    db.refresh(org)

    return org


@router.get("/", response_model=OrganizationList)
def list_organizations(
    include_cancelled: bool = Query(
        False, description="Include the caller's organization when cancelled"
    ),
    q: str | None = Query(None, description="Case-insensitive search on organization name"),
    pagination: PaginationParams = Depends(get_pagination_params),
    db: Session = Depends(get_db),
    current_user: Person = Depends(get_current_user),
):
    """List only the caller's organization. Excludes cancelled by default."""
    filters = [Organization.id == current_user.org_id]
    if not include_cancelled:
        filters.append(Organization.cancelled_at.is_(None))

    if q:
        filters.append(Organization.name.ilike(f"%{q}%"))

    orgs = db.scalars(
        select(Organization)
        .where(*filters)
        .order_by(Organization.id)
        .offset(pagination.offset)
        .limit(pagination.limit)
    ).all()
    total = db.scalar(select(func.count()).select_from(Organization).where(*filters))
    return {
        "items": orgs,
        "total": total,
        "limit": pagination.limit,
        "offset": pagination.offset,
    }


@router.get("/{org_id}", response_model=OrganizationResponse)
def get_organization(
    org_id: str, db: Session = Depends(get_db), current_user: Person = Depends(get_current_user)
):
    """Read the authenticated member's organization only."""
    return _get_scoped_organization(org_id, db, current_user)


@router.put("/{org_id}", response_model=OrganizationResponse)
def update_organization(
    org_id: str,
    org_data: OrganizationUpdate,
    db: Session = Depends(get_db),
    current_admin: Person = Depends(get_current_admin_user),
):
    """Update the authenticated admin's organization and record the actor."""
    org = _get_scoped_organization(org_id, db, current_admin, admin=True)

    # Update fields
    if org_data.name is not None:
        org.name = org_data.name
    if org_data.region is not None:
        org.region = org_data.region
    if org_data.config is not None:
        org.config = org_data.config

    _commit_organization_change(db, org_id, current_admin, AuditAction.ORG_UPDATED)
    db.refresh(org)
    return org


@router.delete("/{org_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_organization(
    org_id: str,
    db: Session = Depends(get_db),
    current_admin: Person = Depends(get_current_admin_user),
):
    """Hard-delete the authenticated admin's organization and related data."""
    org = _get_scoped_organization(org_id, db, current_admin, admin=True)

    db.delete(org)
    _commit_organization_change(db, org_id, current_admin, AuditAction.BULK_DELETE)
    return None


@router.post("/{org_id}/cancel", response_model=OrganizationResponse)
def cancel_organization(
    org_id: str,
    http_request: Request,
    current_admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Soft-cancel the organization (admin only).

    Sets `cancelled_at` to now and schedules a 30-day data-retention window
    via `data_retention_until`. The org is excluded from the default list
    until restored.
    """
    org = _get_scoped_organization(org_id, db, current_admin, admin=True)

    now = utcnow()
    org.cancelled_at = now
    org.data_retention_until = now + timedelta(days=30)
    _commit_organization_change(
        db,
        org_id,
        current_admin,
        AuditAction.ORG_CANCELLED,
        http_request,
        {"data_retention_until": org.data_retention_until.isoformat()},
    )
    db.refresh(org)
    return org


@router.post("/{org_id}/restore", response_model=OrganizationResponse)
def restore_organization(
    org_id: str,
    http_request: Request,
    current_admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Restore a cancelled organization (admin only). Clears cancellation fields."""
    org = _get_scoped_organization(org_id, db, current_admin, admin=True)

    org.cancelled_at = None
    org.data_retention_until = None
    org.deletion_scheduled_at = None
    _commit_organization_change(db, org_id, current_admin, AuditAction.ORG_RESTORED, http_request)
    db.refresh(org)
    return org
