"""Billing and subscription management endpoints."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload

from api.database import get_db
from api.dependencies import get_current_admin_user, verify_org_member
from api.models import Person
from api.schemas.billing import (
    CancelRequest,
    DowngradeRequest,
    SubscriptionResponse,
    TrialRequest,
    UpgradeRequest,
)
from api.services.billing_service import BillingService
from api.services.stripe_service import StripeService
from api.services.usage_service import UsageService

router = APIRouter(tags=["billing"])


@router.get("/billing/subscription")
def get_subscription(
    org_id: str = Query(..., description="Organization ID"),
    current_user: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Get current subscription details for organization.

    Returns subscription tier, usage metrics, and billing information.

    Requires:
        - User must be an authenticated admin of the organization

    Returns:
        dict: Subscription details with usage metrics
        {
            "subscription": SubscriptionResponse,
            "usage": UsageSummaryResponse,
            "next_invoice": Optional[dict]
        }
    """
    # Verify user belongs to organization
    verify_org_member(current_user, org_id)

    # Get subscription
    billing_service = BillingService(db)
    subscription = billing_service.get_subscription(org_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="No subscription found for organization")

    # Get usage summary
    usage_service = UsageService(db)
    usage_summary = usage_service.get_usage_summary(org_id)

    # Get billing history for next invoice (if on paid plan)
    next_invoice = None
    if subscription.plan_tier != "free" and subscription.current_period_end:
        next_invoice = {
            "due_date": subscription.current_period_end.isoformat(),
            "amount": "Based on plan tier",  # Placeholder
            "status": subscription.status,
        }

    return {
        "subscription": SubscriptionResponse.from_orm(subscription),
        "usage": usage_summary,
        "next_invoice": next_invoice,
    }


@router.post("/billing/subscription/upgrade")
def upgrade_subscription(
    request: UpgradeRequest,
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Upgrade organization to paid plan.

    This endpoint:
    1. Creates Stripe checkout session for payment collection
    2. Upgrades subscription when payment succeeds
    3. Records billing history and audit trail
    4. Updates usage limits to new plan tier
    5. Sends confirmation email (future)

    Requires:
        - User must be admin
        - User must belong to the organization
        - Organization must have active free plan

    Request Body:
        {
            "org_id": "org_123",
            "plan_tier": "starter",
            "billing_cycle": "monthly",
            "payment_method_id": "pm_xxx",
            "trial_days": 14
        }

    Returns:
        dict: Checkout session URL for payment
        {
            "success": true,
            "checkout_url": "https://checkout.stripe.com/...",
            "session_id": "cs_xxx",
            "message": "Checkout session created"
        }
    """
    # Verify admin belongs to organization
    verify_org_member(admin, request.org_id)

    # Get current subscription
    billing_service = BillingService(db)
    subscription = billing_service.get_subscription(request.org_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="No subscription found for organization")

    # Only allow upgrading from free tier for now
    if subscription.plan_tier != "free":
        raise HTTPException(
            status_code=400,
            detail=f"Organization already has {subscription.plan_tier} plan. Use change plan endpoint to switch plans.",
        )

    # Construct Stripe price ID from plan tier and billing cycle
    price_id = f"price_{request.plan_tier}_{request.billing_cycle}"

    # Create Stripe checkout session
    stripe_service = StripeService(db)
    result = stripe_service.create_checkout_session(
        org_id=request.org_id,
        price_id=price_id,
        success_url="https://signupflow.io/app/billing?session_id={CHECKOUT_SESSION_ID}",
        cancel_url="https://signupflow.io/app/billing?cancelled=true",
        trial_days=request.trial_days,
    )

    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["message"])

    return result


@router.post("/billing/subscription/checkout-success")
def handle_checkout_success(
    session_id: str = Query(..., description="Stripe checkout session ID"),
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Handle successful checkout completion.

    This is called when user returns from Stripe checkout page after successful payment.
    The actual subscription update happens via webhook, but this endpoint can be used
    to retrieve updated subscription status.

    Requires:
        - User must be admin
        - Valid Stripe session ID

    Returns:
        dict: Updated subscription details
    """
    # In production, we would verify the session_id with Stripe
    # and ensure it matches the user's organization
    # For now, just return the current subscription

    # Get organization from session (this would come from Stripe session metadata)
    # For now, use admin's org_id
    org_id = admin.org_id

    billing_service = BillingService(db)
    subscription = billing_service.get_subscription(org_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="No subscription found")

    usage_service = UsageService(db)
    usage_summary = usage_service.get_usage_summary(org_id)

    return {
        "success": True,
        "subscription": SubscriptionResponse.from_orm(subscription),
        "usage": usage_summary,
        "message": "Subscription updated successfully",
    }


@router.post("/billing/subscription/trial")
def start_trial(
    request: TrialRequest,
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Start a 14-day trial of a paid plan.

    This endpoint:
    1. Validates organization is on free plan
    2. Updates subscription to trial status
    3. Sets trial_end_date to 14 days from now
    4. Updates usage limits to trial plan tier
    5. Records subscription event for audit trail
    6. Sends trial welcome email (future)

    Requires:
        - User must be admin
        - User must belong to the organization
        - Organization must have active free plan

    Request Body:
        {
            "org_id": "org_123",
            "plan_tier": "starter",
            "trial_days": 14
        }

    Returns:
        dict: Trial subscription details
        {
            "success": true,
            "subscription": SubscriptionResponse,
            "trial_end_date": "2025-11-06T...",
            "message": "Started 14-day trial of starter plan"
        }
    """
    # Verify admin belongs to organization
    verify_org_member(admin, request.org_id)

    # Start trial via BillingService
    billing_service = BillingService(db)
    result = billing_service.start_trial(
        org_id=request.org_id,
        plan_tier=request.plan_tier,
        trial_days=request.trial_days,
        admin_id=admin.id,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    # Get updated usage summary
    usage_service = UsageService(db)
    usage_summary = usage_service.get_usage_summary(request.org_id)

    return {
        "success": True,
        "subscription": SubscriptionResponse.from_orm(result["subscription"]),
        "usage": usage_summary,
        "trial_end_date": result["trial_end_date"].isoformat(),
        "message": result["message"],
    }


@router.post("/billing/subscription/downgrade")
def downgrade_subscription(
    request: DowngradeRequest,
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Schedule subscription downgrade to execute at period end.

    This endpoint:
    1. Validates downgrade is to lower tier
    2. Schedules downgrade for current period end
    3. Calculates credit for unused time
    4. Stores pending downgrade in subscription.pending_downgrade
    5. Records subscription event for audit trail
    6. Sends downgrade confirmation email (future)

    Requires:
        - User must be admin
        - User must belong to the organization
        - Organization must have active paid subscription
        - New plan tier must be lower than current tier

    Request Body:
        {
            "org_id": "org_123",
            "new_plan_tier": "starter",
            "reason": "Cost reduction"
        }

    Returns:
        dict: Downgrade scheduled details
        {
            "success": true,
            "subscription": SubscriptionResponse,
            "pending_downgrade": {
                "new_plan_tier": "starter",
                "effective_date": "2025-11-23",
                "credit_amount_cents": 5000,
                "reason": "Cost reduction"
            },
            "message": "Downgrade scheduled for end of billing period"
        }
    """
    # Verify admin belongs to organization
    verify_org_member(admin, request.org_id)

    # Schedule downgrade via BillingService
    billing_service = BillingService(db)
    result = billing_service.downgrade_subscription(
        org_id=request.org_id,
        new_plan_tier=request.new_plan_tier,
        reason=request.reason,
        admin_id=admin.id,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    # Get updated usage summary
    usage_service = UsageService(db)
    usage_summary = usage_service.get_usage_summary(request.org_id)

    return {
        "success": True,
        "subscription": SubscriptionResponse.from_orm(result["subscription"]),
        "usage": usage_summary,
        "pending_downgrade": result["pending_downgrade"],
        "message": result["message"],
    }


@router.post("/billing/subscription/cancel-downgrade")
def cancel_downgrade(
    org_id: str = Query(..., description="Organization ID"),
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Cancel scheduled downgrade.

    This endpoint:
    1. Verifies pending downgrade exists
    2. Clears pending_downgrade field from subscription
    3. Records subscription event for audit trail

    Requires:
        - User must be admin
        - User must belong to the organization
        - Organization must have pending downgrade scheduled

    Returns:
        dict: Cancellation confirmation
        {
            "success": true,
            "subscription": SubscriptionResponse,
            "message": "Downgrade cancelled"
        }
    """
    # Verify admin belongs to organization
    verify_org_member(admin, org_id)

    # Get subscription
    billing_service = BillingService(db)
    subscription = billing_service.get_subscription(org_id)

    if not subscription:
        raise HTTPException(status_code=404, detail="No subscription found for organization")

    # Check if pending downgrade exists
    if not subscription.pending_downgrade:
        raise HTTPException(status_code=400, detail="No pending downgrade to cancel")

    # Store pending downgrade details for event recording
    pending = subscription.pending_downgrade
    new_plan_tier = pending.get("new_plan_tier")

    # Clear pending downgrade
    subscription.pending_downgrade = None
    db.commit()
    db.refresh(subscription)

    # Record subscription event

    from api.models import SubscriptionEvent

    event = SubscriptionEvent(
        org_id=org_id,
        event_type="downgrade_cancelled",
        new_plan=subscription.plan_tier,  # Stays on current plan
        previous_plan=subscription.plan_tier,
        admin_id=admin.id,
        notes=f"Cancelled scheduled downgrade to {new_plan_tier}",
    )
    db.add(event)
    db.commit()

    # Get updated usage summary
    usage_service = UsageService(db)
    usage_summary = usage_service.get_usage_summary(org_id)

    return {
        "success": True,
        "subscription": SubscriptionResponse.from_orm(subscription),
        "usage": usage_summary,
        "message": "Downgrade cancelled successfully",
    }


@router.post("/billing/subscription/cancel")
def cancel_subscription(
    request: CancelRequest,
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Cancel subscription with service continuing until period end.

    This endpoint:
    1. Cancels subscription in Stripe (at period end by default)
    2. Service continues until current period ends
    3. Organization downgraded to Free plan at period end
    4. Data retained for 30 days after cancellation
    5. Records cancellation event for audit trail
    6. Sends cancellation confirmation email (future)

    Requires:
        - User must be admin
        - User must belong to the organization
        - Organization must have active paid subscription

    Request Body:
        {
            "org_id": "org_123",
            "immediately": false,
            "reason": "Cost reduction",
            "feedback": "Great service, just downsizing"
        }

    Returns:
        dict: Cancellation details
        {
            "success": true,
            "subscription": SubscriptionResponse,
            "period_end": "2025-11-23T...",
            "data_retention_until": "2025-12-23T...",
            "message": "Subscription will cancel at period end"
        }
    """
    # Verify admin belongs to organization
    verify_org_member(admin, request.org_id)

    # Cancel subscription via BillingService
    billing_service = BillingService(db)
    result = billing_service.cancel_subscription(
        org_id=request.org_id,
        reason=request.reason,
        feedback=request.feedback,
        admin_id=admin.id,
        at_period_end=not request.immediately,  # Invert immediately flag
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    # Get updated usage summary
    usage_service = UsageService(db)
    usage_summary = usage_service.get_usage_summary(request.org_id)

    return {
        "success": True,
        "subscription": SubscriptionResponse.from_orm(result["subscription"]),
        "usage": usage_summary,
        "period_end": result["period_end"].isoformat() if result["period_end"] else None,
        "data_retention_until": result["data_retention_until"].isoformat()
        if result["data_retention_until"]
        else None,
        "message": result["message"],
    }


@router.post("/billing/subscription/reactivate")
def reactivate_subscription(
    org_id: str = Query(..., description="Organization ID"),
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Reactivate a cancelled subscription within data retention period.

    This endpoint:
    1. Validates organization is within 30-day retention window
    2. Restores subscription to previous plan tier
    3. Clears cancellation and retention timestamps
    4. Records reactivation event for audit trail
    5. Sends reactivation confirmation email (future)

    Requires:
        - User must be admin
        - User must belong to the organization
        - Organization must be within data retention period
        - Subscription must be cancelled

    Query Parameters:
        org_id: Organization ID

    Returns:
        dict: Reactivation confirmation
        {
            "success": true,
            "subscription": SubscriptionResponse,
            "usage": UsageSummaryResponse,
            "message": "Subscription reactivated successfully..."
        }
    """
    # Verify admin belongs to organization
    verify_org_member(admin, org_id)

    # Reactivate subscription via BillingService
    billing_service = BillingService(db)
    result = billing_service.reactivate_subscription(org_id=org_id, admin_id=admin.id)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    # Get updated usage summary
    usage_service = UsageService(db)
    usage_summary = usage_service.get_usage_summary(org_id)

    return {
        "success": True,
        "subscription": SubscriptionResponse.from_orm(result["subscription"]),
        "usage": usage_summary,
        "message": result["message"],
    }


# ============================================================================
# Payment Methods Management (US8)
# ============================================================================


@router.get("/billing/payment-methods")
def get_payment_methods(
    org_id: str = Query(..., description="Organization ID"),
    current_user: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Get organization's payment methods from Stripe.

    Returns list of payment methods with card details, expiration, and primary status.

    Requires:
        - User must be an authenticated admin of the organization

    Query Parameters:
        org_id: Organization ID

    Returns:
        {
            "success": true,
            "payment_methods": [
                {
                    "id": "pm_xxx",
                    "type": "card",
                    "card": {
                        "brand": "visa",
                        "last4": "4242",
                        "exp_month": 12,
                        "exp_year": 2025
                    },
                    "is_default": true
                }
            ]
        }
    """
    verify_org_member(current_user, org_id)

    from api.services.stripe_service import StripeService

    stripe_service = StripeService(db)
    result = stripe_service.list_payment_methods(org_id)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return {"success": True, "payment_methods": result["payment_methods"]}


@router.post("/billing/payment-methods")
def add_payment_method(
    payment_method_id: str = Query(..., description="Stripe payment method ID"),
    org_id: str = Query(..., description="Organization ID"),
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Add new payment method to organization via Stripe.

    Payment method must be created client-side using Stripe.js before calling this endpoint.

    Requires:
        - User must be admin
        - User must belong to the organization

    Query Parameters:
        payment_method_id: Stripe payment method ID (created client-side)
        org_id: Organization ID

    Returns:
        {
            "success": true,
            "message": "Payment method added successfully"
        }
    """
    verify_org_member(admin, org_id)

    from api.services.stripe_service import StripeService

    stripe_service = StripeService(db)
    result = stripe_service.attach_payment_method(org_id, payment_method_id)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return {"success": True, "message": result["message"]}


@router.delete("/billing/payment-methods/{payment_method_id}")
def remove_payment_method(
    payment_method_id: str,
    org_id: str = Query(..., description="Organization ID"),
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Remove payment method from organization.

    Requires:
        - User must be admin
        - User must belong to the organization
        - Cannot remove the only payment method on active paid subscription

    Path Parameters:
        payment_method_id: Stripe payment method ID to remove

    Query Parameters:
        org_id: Organization ID

    Returns:
        {
            "success": true,
            "message": "Payment method removed successfully"
        }
    """
    verify_org_member(admin, org_id)

    from api.services.stripe_service import StripeService

    stripe_service = StripeService(db)
    result = stripe_service.detach_payment_method(org_id, payment_method_id)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return {"success": True, "message": result["message"]}


@router.put("/billing/payment-methods/{payment_method_id}/primary")
def set_primary_payment_method(
    payment_method_id: str,
    org_id: str = Query(..., description="Organization ID"),
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Set payment method as primary (default) for organization.

    Requires:
        - User must be admin
        - User must belong to the organization

    Path Parameters:
        payment_method_id: Stripe payment method ID to set as primary

    Query Parameters:
        org_id: Organization ID

    Returns:
        {
            "success": true,
            "message": "Primary payment method updated successfully"
        }
    """
    verify_org_member(admin, org_id)

    from api.services.stripe_service import StripeService

    stripe_service = StripeService(db)
    result = stripe_service.set_default_payment_method(org_id, payment_method_id)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return {"success": True, "message": result["message"]}


@router.get("/billing/history")
def get_billing_history(
    org_id: str = Query(..., description="Organization ID"),
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(50, ge=1, le=100, description="Records per page (default: 50)"),
    current_user: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Get organization's billing history with pagination.

    Returns list of billing events (charges, refunds, subscription changes).

    Requires:
        - User must be an authenticated admin of the organization

    Query Parameters:
        org_id: Organization ID
        page: Page number (default: 1)
        limit: Records per page (default: 50, max: 100)

    Returns:
        {
            "success": true,
            "history": [
                {
                    "id": 123,
                    "event_type": "charge",
                    "amount_cents": 2900,
                    "currency": "usd",
                    "payment_status": "succeeded",
                    "event_timestamp": "2025-10-23T10:00:00Z",
                    "description": "Payment for starter plan",
                    "stripe_invoice_id": "in_xxx"
                }
            ],
            "pagination": {
                "page": 1,
                "limit": 50,
                "total": 45,
                "pages": 1
            }
        }
    """
    verify_org_member(current_user, org_id)

    from api.models import BillingHistory

    # Calculate offset
    offset = (page - 1) * limit

    # Query billing history with pagination (optimized with index on org_id, event_timestamp)
    history_query = (
        db.query(BillingHistory)
        .filter(BillingHistory.org_id == org_id)
        .order_by(BillingHistory.event_timestamp.desc())
    )

    total = history_query.count()
    history_records = history_query.offset(offset).limit(limit).all()

    # Format history records
    history = []
    for record in history_records:
        history.append(
            {
                "id": record.id,
                "event_type": record.event_type,
                "amount_cents": record.amount_cents,
                "currency": record.currency,
                "payment_status": record.payment_status,
                "event_timestamp": record.event_timestamp.isoformat()
                if record.event_timestamp
                else None,
                "description": record.description or "",
                "stripe_invoice_id": record.stripe_invoice_id,
            }
        )

    return {
        "success": True,
        "history": history,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "pages": (total + limit - 1) // limit,  # Ceiling division
        },
    }


@router.get("/billing/invoices/{billing_history_id}/pdf")
def download_invoice_pdf(
    billing_history_id: str,
    format: str = Query("html", pattern="^(pdf|html)$", description="Output format (pdf or html)"),
    current_user: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """
    Generate and download invoice PDF for billing history record.

    Returns PDF file for download or HTML preview.

    Requires:
        - User must be an authenticated admin of the organization

    Path Parameters:
        billing_history_id: Billing history record ID

    Query Parameters:
        format: Output format - "pdf" (text-based) or "html" (styled template)

    Returns:
        PDF file download or HTML response
    """
    from fastapi.responses import HTMLResponse, StreamingResponse

    from api.models import BillingHistory, Organization
    from api.utils.invoice_generator import generate_invoice_pdf, generate_invoice_pdf_html

    # Get billing history record
    billing_record = (
        db.query(BillingHistory)
        .filter(
            BillingHistory.id == billing_history_id,
            BillingHistory.org_id == current_user.org_id,
        )
        .first()
    )

    if not billing_record:
        raise HTTPException(status_code=404, detail="Invoice not found")

    # Verify user belongs to organization
    verify_org_member(current_user, billing_record.org_id)

    # Get organization details (with eager-loaded subscription)
    org = (
        db.query(Organization)
        .options(joinedload(Organization.subscription))
        .filter(Organization.id == billing_record.org_id)
        .first()
    )

    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    if format == "html":
        # Generate HTML invoice
        html_content = generate_invoice_pdf_html(
            billing_history_id=billing_record.id,
            org_name=org.name,
            event_type=billing_record.event_type,
            plan_tier=billing_record.plan_tier or "free",
            amount_cents=billing_record.amount_cents or 0,
            created_at=billing_record.created_at,
            description=billing_record.description,
            org_address=getattr(org, "region", None),
            invoice_number=f"INV-{billing_record.id[:8].upper()}",
        )

        return HTMLResponse(content=html_content)

    else:
        # Generate simple text-based PDF
        pdf_buffer = generate_invoice_pdf(
            billing_history_id=billing_record.id,
            org_name=org.name,
            event_type=billing_record.event_type,
            plan_tier=billing_record.plan_tier or "free",
            amount_cents=billing_record.amount_cents or 0,
            created_at=billing_record.created_at,
            description=billing_record.description,
        )

        filename = f"invoice_{billing_record.id[:8]}.txt"

        return StreamingResponse(
            pdf_buffer,
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )


@router.post("/billing/portal")
def create_billing_portal_session(
    org_id: str = Query(..., description="Organization ID"),
    admin: Person = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """
    Create Stripe billing portal session for self-service management.

    The Stripe billing portal allows customers to:
    - Update payment methods
    - View invoices and payment history
    - Update billing email
    - Cancel subscription

    Requires:
        - User must be admin of the organization
        - Organization must have Stripe customer ID

    Args:
        org_id: Organization ID
        return_url: URL to return to after portal session (from request body)

    Returns:
        dict: {"success": bool, "url": str}
    """
    verify_org_member(admin, org_id)

    stripe_service = StripeService(db)
    result = stripe_service.create_billing_portal_session(org_id)

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["message"])

    return {"success": True, "url": result["url"]}
