"""Stripe integration service for subscription management.

This service handles all Stripe API interactions for billing operations.
It coordinates between Stripe and the local database.

Example Usage:
    from api.services.stripe_service import StripeService

    stripe_service = StripeService(db)
    result = stripe_service.upgrade_to_paid(
        org_id="org_123",
        price_id="price_starter_monthly",
        trial_days=14
    )
"""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from api.logging_config import logger
from api.models import Organization, PaymentMethod, Subscription
from api.utils.stripe_client import StripeClient


class StripeService:
    """Service for Stripe-specific billing operations."""

    def __init__(self, db: Session):
        """
        Initialize Stripe service.

        Args:
            db: SQLAlchemy database session
        """
        self.db = db
        self.stripe_client = StripeClient()

    def upgrade_to_paid(
        self, org_id: str, price_id: str, trial_days: int | None = 14
    ) -> dict[str, Any]:
        """
        Upgrade organization from free to paid plan.

        This creates a Stripe customer and subscription, then updates local database.

        Args:
            org_id: Organization ID
            price_id: Stripe price ID (price_xxx)
            trial_days: Number of trial days (default: 14)

        Returns:
            dict: Result with success status and subscription details
                {
                    "success": bool,
                    "subscription": Subscription object,
                    "message": str,
                    "stripe_subscription_id": str
                }

        Example:
            result = stripe_service.upgrade_to_paid(
                org_id="org_123",
                price_id="price_starter_monthly",
                trial_days=14
            )
            if result["success"]:
                print(f"Subscription ID: {result['stripe_subscription_id']}")
        """
        try:
            # Get organization and current subscription (with eager loading)
            org = (
                self.db.query(Organization)
                .options(joinedload(Organization.subscription))
                .filter(Organization.id == org_id)
                .first()
            )
            if not org:
                return {"success": False, "message": "Organization not found"}

            subscription = self.db.query(Subscription).filter(Subscription.org_id == org_id).first()

            if not subscription:
                return {"success": False, "message": "No subscription record found"}

            # Create or retrieve Stripe customer
            if not subscription.stripe_customer_id:
                admin = next((p for p in org.people if "admin" in (p.roles or [])), None)
                customer_email = admin.email if admin else f"{org_id}@signupflow.io"

                customer_result = self.stripe_client.create_customer(
                    org_id=org_id, email=customer_email, name=org.name
                )

                if not customer_result["success"]:
                    return {"success": False, "message": customer_result["message"]}

                subscription.stripe_customer_id = customer_result["customer_id"]

            # Create Stripe subscription
            subscription_result = self.stripe_client.create_subscription(
                customer_id=subscription.stripe_customer_id,
                price_id=price_id,
                trial_days=trial_days,
                metadata={"org_id": org_id},
            )

            if not subscription_result["success"]:
                return {"success": False, "message": subscription_result["message"]}

            # Extract Stripe subscription object
            stripe_subscription = subscription_result["subscription"]

            # Determine plan tier from price_id
            plan_tier = self._get_tier_from_price_id(price_id)
            billing_cycle = "monthly" if "monthly" in price_id else "annual"

            # Update local subscription record
            subscription.stripe_subscription_id = stripe_subscription["id"]
            subscription.plan_tier = plan_tier
            subscription.billing_cycle = billing_cycle
            subscription.status = stripe_subscription["status"]  # "trialing" or "active"
            subscription.current_period_start = datetime.fromtimestamp(
                stripe_subscription["current_period_start"]
            )
            subscription.current_period_end = datetime.fromtimestamp(
                stripe_subscription["current_period_end"]
            )

            if stripe_subscription.get("trial_end"):
                subscription.trial_end_date = datetime.fromtimestamp(
                    stripe_subscription["trial_end"]
                )

            self.db.commit()
            self.db.refresh(subscription)

            logger.info(
                f"Upgraded org {org_id} to {plan_tier} plan "
                f"(Stripe subscription: {stripe_subscription['id']})"
            )

            return {
                "success": True,
                "subscription": subscription,
                "stripe_subscription_id": stripe_subscription["id"],
                "message": f"Successfully upgraded to {plan_tier} plan",
            }

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error upgrading org {org_id} to paid plan: {e}")
            return {"success": False, "message": f"Unexpected error: {str(e)}"}

    def change_plan(self, org_id: str, new_price_id: str) -> dict[str, Any]:
        """
        Change organization's subscription plan (upgrade or downgrade).

        Args:
            org_id: Organization ID
            new_price_id: New Stripe price ID

        Returns:
            dict: Result with success status and details
        """
        try:
            subscription = self.db.query(Subscription).filter(Subscription.org_id == org_id).first()

            if not subscription or not subscription.stripe_subscription_id:
                return {"success": False, "message": "No active paid subscription found"}

            # Update Stripe subscription
            update_result = self.stripe_client.update_subscription(
                subscription_id=subscription.stripe_subscription_id, price_id=new_price_id
            )

            if not update_result["success"]:
                return {"success": False, "message": update_result["message"]}

            # Extract updated subscription object
            updated_stripe_sub = update_result["subscription"]

            # Update local database
            previous_plan = subscription.plan_tier
            new_plan = self._get_tier_from_price_id(new_price_id)
            billing_cycle = "monthly" if "monthly" in new_price_id else "annual"

            subscription.plan_tier = new_plan
            subscription.billing_cycle = billing_cycle
            subscription.status = updated_stripe_sub["status"]

            self.db.commit()
            self.db.refresh(subscription)

            logger.info(f"Changed plan for org {org_id} from {previous_plan} to {new_plan}")

            return {
                "success": True,
                "subscription": subscription,
                "message": f"Plan changed from {previous_plan} to {new_plan}",
            }

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error changing plan for org {org_id}: {e}")
            return {"success": False, "message": str(e)}

    def update_subscription_billing_cycle(
        self, stripe_subscription_id: str, new_price_id: str, prorated_amount_cents: int
    ) -> dict[str, Any]:
        """
        Update subscription billing cycle in Stripe.

        Args:
            stripe_subscription_id: Stripe subscription ID
            new_price_id: New Stripe price ID (with new billing cycle)
            prorated_amount_cents: Prorated amount to charge/credit

        Returns:
            dict: Result with success status
                {
                    "success": bool,
                    "stripe_subscription": dict,
                    "message": str
                }

        Example:
            result = stripe_service.update_subscription_billing_cycle(
                stripe_subscription_id="sub_123",
                new_price_id="price_starter_annual",
                prorated_amount_cents=5000  # Charge $50 prorated
            )
        """
        try:
            # Update Stripe subscription with new price
            update_result = self.stripe_client.update_subscription(
                subscription_id=stripe_subscription_id,
                price_id=new_price_id,
                proration_behavior="always_invoice",  # Create invoice for proration
            )

            if not update_result["success"]:
                return {"success": False, "message": update_result["message"]}

            updated_subscription = update_result["subscription"]

            logger.info(
                f"Updated billing cycle for subscription {stripe_subscription_id} "
                f"to {new_price_id} (prorated: ${abs(prorated_amount_cents)/100:.2f})"
            )

            return {
                "success": True,
                "stripe_subscription": updated_subscription,
                "message": "Billing cycle updated in Stripe",
            }

        except Exception as e:
            logger.error(
                f"Error updating billing cycle for subscription {stripe_subscription_id}: {e}"
            )
            return {"success": False, "message": f"Failed to update billing cycle: {str(e)}"}

    def cancel_subscription(self, org_id: str, at_period_end: bool = True) -> dict[str, Any]:
        """
        Cancel organization's subscription.

        Args:
            org_id: Organization ID
            at_period_end: Cancel at period end (True) or immediately (False)

        Returns:
            dict: Result with success status
        """
        try:
            subscription = self.db.query(Subscription).filter(Subscription.org_id == org_id).first()

            if not subscription or not subscription.stripe_subscription_id:
                return {"success": False, "message": "No active subscription found"}

            # Cancel in Stripe
            cancel_result = self.stripe_client.cancel_subscription(
                subscription_id=subscription.stripe_subscription_id, at_period_end=at_period_end
            )

            if not cancel_result["success"]:
                return {"success": False, "message": cancel_result["message"]}

            # Update local database
            if at_period_end:
                subscription.cancel_at_period_end = True
                message = "Subscription will cancel at period end"
            else:
                subscription.status = "cancelled"
                subscription.plan_tier = "free"
                message = "Subscription cancelled immediately"

            self.db.commit()
            self.db.refresh(subscription)

            logger.info(f"Cancelled subscription for org {org_id} (at_period_end={at_period_end})")

            return {"success": True, "subscription": subscription, "message": message}

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error cancelling subscription for org {org_id}: {e}")
            return {"success": False, "message": str(e)}

    def save_payment_method(
        self, org_id: str, payment_method_id: str, card_details: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Save payment method for organization.

        Args:
            org_id: Organization ID
            payment_method_id: Stripe payment method ID
            card_details: Card details from Stripe (brand, last4, exp_month, exp_year)

        Returns:
            dict: Result with success status
        """
        try:
            subscription = self.db.query(Subscription).filter(Subscription.org_id == org_id).first()

            if not subscription or not subscription.stripe_customer_id:
                return {"success": False, "message": "No Stripe customer found"}

            # Attach payment method in Stripe
            attach_result = self.stripe_client.attach_payment_method(
                customer_id=subscription.stripe_customer_id,
                payment_method_id=payment_method_id,
                set_as_default=True,
            )

            if not attach_result["success"]:
                return {"success": False, "message": attach_result["message"]}

            # Save to local database
            payment_method = PaymentMethod(
                org_id=org_id,
                stripe_payment_method_id=payment_method_id,
                card_brand=card_details.get("brand"),
                card_last4=card_details.get("last4"),
                exp_month=card_details.get("exp_month"),
                exp_year=card_details.get("exp_year"),
                is_primary=True,
            )

            # Set other payment methods as non-primary
            self.db.query(PaymentMethod).filter(
                PaymentMethod.org_id == org_id, PaymentMethod.is_primary is True
            ).update({"is_primary": False})

            self.db.add(payment_method)
            self.db.commit()

            logger.info(f"Saved payment method for org {org_id}")

            return {
                "success": True,
                "payment_method": payment_method,
                "message": "Payment method saved successfully",
            }

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error saving payment method for org {org_id}: {e}")
            return {"success": False, "message": str(e)}

    def create_checkout_session(
        self,
        org_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        trial_days: int | None = 14,
    ) -> dict[str, Any]:
        """
        Create a Stripe Checkout session for payment collection.

        This creates a hosted checkout page where users can enter payment details.

        Args:
            org_id: Organization ID
            price_id: Stripe price ID
            success_url: URL to redirect after successful payment
            cancel_url: URL to redirect if user cancels
            trial_days: Number of trial days (default: 14)

        Returns:
            dict: Result with checkout session URL
                {
                    "success": bool,
                    "checkout_url": str,
                    "session_id": str,
                    "message": str
                }

        Example:
            result = stripe_service.create_checkout_session(
                org_id="org_123",
                price_id="price_starter_monthly",
                success_url="https://signupflow.io/billing/success",
                cancel_url="https://signupflow.io/billing/cancel",
                trial_days=14
            )
            if result["success"]:
                return redirect(result["checkout_url"])
        """
        try:
            # Get or create Stripe customer
            subscription = self.db.query(Subscription).filter(Subscription.org_id == org_id).first()

            if not subscription:
                return {"success": False, "message": "No subscription record found"}

            customer_id = subscription.stripe_customer_id

            # Create customer if doesn't exist
            if not customer_id:
                org = (
                    self.db.query(Organization)
                    .options(joinedload(Organization.subscription))
                    .filter(Organization.id == org_id)
                    .first()
                )
                if not org:
                    return {"success": False, "message": "Organization not found"}

                admin = next((p for p in org.people if "admin" in (p.roles or [])), None)
                customer_email = admin.email if admin else f"{org_id}@signupflow.io"

                customer_result = self.stripe_client.create_customer(
                    org_id=org_id, email=customer_email, name=org.name, metadata={"org_id": org_id}
                )

                if not customer_result["success"]:
                    return {"success": False, "message": customer_result["message"]}

                customer_id = customer_result["customer_id"]
                subscription.stripe_customer_id = customer_id
                self.db.commit()

            # Create checkout session
            import stripe

            checkout_session = stripe.checkout.Session.create(
                customer=customer_id,
                mode="subscription",
                line_items=[{"price": price_id, "quantity": 1}],
                subscription_data={
                    "trial_period_days": trial_days if trial_days and trial_days > 0 else None,
                    "metadata": {
                        "org_id": org_id,
                        "plan_tier": self._get_tier_from_price_id(price_id),
                    },
                },
                success_url=success_url,
                cancel_url=cancel_url,
                metadata={"org_id": org_id},
            )

            logger.info(f"Created checkout session for org {org_id}: {checkout_session.id}")

            return {
                "success": True,
                "checkout_url": checkout_session.url,
                "session_id": checkout_session.id,
                "message": "Checkout session created",
            }

        except Exception as e:
            logger.error(f"Error creating checkout session for org {org_id}: {e}")
            return {"success": False, "message": f"Failed to create checkout session: {str(e)}"}

    def _get_tier_from_price_id(self, price_id: str) -> str:
        """
        Extract plan tier from Stripe price ID.

        Args:
            price_id: Stripe price ID (e.g., "price_starter_monthly")

        Returns:
            str: Plan tier (starter, pro, enterprise)
        """
        price_lower = price_id.lower()

        if "starter" in price_lower:
            return "starter"
        elif "pro" in price_lower:
            return "pro"
        elif "enterprise" in price_lower:
            return "enterprise"
        else:
            logger.warning(f"Unknown price ID format: {price_id}, defaulting to starter")
            return "starter"

    def apply_customer_credit(
        self, customer_id: str, amount_cents: int, description: str = "Account credit"
    ) -> dict[str, Any]:
        """
        Apply credit to Stripe customer balance.

        This adds a customer balance transaction that can be used to credit
        future invoices automatically.

        Args:
            customer_id: Stripe customer ID
            amount_cents: Credit amount in cents (positive number)
            description: Description of the credit

        Returns:
            dict: Result of credit application
            {
                "success": bool,
                "transaction_id": str,
                "amount_cents": int,
                "message": str
            }
        """
        try:
            import stripe

            # Create negative balance transaction to add credit
            # Stripe customer balance uses negative values for credits
            balance_transaction = stripe.Customer.create_balance_transaction(
                customer_id,
                amount=-abs(amount_cents),  # Negative = credit
                currency="usd",
                description=description,
            )

            logger.info(
                f"Applied ${amount_cents/100:.2f} credit to customer {customer_id} "
                f"(transaction {balance_transaction.id})"
            )

            return {
                "success": True,
                "transaction_id": balance_transaction.id,
                "amount_cents": amount_cents,
                "message": f"Applied ${amount_cents/100:.2f} credit to customer balance",
            }

        except Exception as e:
            logger.error(f"Error applying credit to customer {customer_id}: {e}")
            return {"success": False, "message": f"Failed to apply credit: {str(e)}"}

    def list_payment_methods(self, org_id: str) -> dict[str, Any]:
        """
        List all payment methods for organization's Stripe customer.

        Returns payment methods with card details, expiration, and default status.

        Args:
            org_id: Organization ID

        Returns:
            dict: Payment methods list
            {
                "success": bool,
                "payment_methods": List[dict],
                "message": str
            }
        """
        try:
            import stripe

            from api.models import Subscription

            # Get subscription to find customer ID
            subscription = self.db.query(Subscription).filter(Subscription.org_id == org_id).first()

            if not subscription or not subscription.stripe_customer_id:
                return {
                    "success": True,
                    "payment_methods": [],
                    "message": "No payment methods found (no Stripe customer)",
                }

            # List payment methods for customer
            payment_methods = stripe.PaymentMethod.list(
                customer=subscription.stripe_customer_id, type="card"
            )

            # Get customer to check default payment method
            customer = stripe.Customer.retrieve(subscription.stripe_customer_id)
            default_pm_id = customer.invoice_settings.default_payment_method

            # Format payment methods
            formatted_pms = []
            for pm in payment_methods.data:
                formatted_pms.append(
                    {
                        "id": pm.id,
                        "type": pm.type,
                        "card": {
                            "brand": pm.card.brand,
                            "last4": pm.card.last4,
                            "exp_month": pm.card.exp_month,
                            "exp_year": pm.card.exp_year,
                        },
                        "is_default": pm.id == default_pm_id,
                    }
                )

            logger.info(f"Retrieved {len(formatted_pms)} payment methods for org {org_id}")

            return {
                "success": True,
                "payment_methods": formatted_pms,
                "message": f"Found {len(formatted_pms)} payment methods",
            }

        except Exception as e:
            logger.error(f"Error listing payment methods for org {org_id}: {e}")
            return {
                "success": False,
                "payment_methods": [],
                "message": f"Failed to list payment methods: {str(e)}",
            }

    def attach_payment_method(self, org_id: str, payment_method_id: str) -> dict[str, Any]:
        """
        Attach payment method to organization's Stripe customer.

        Args:
            org_id: Organization ID
            payment_method_id: Stripe payment method ID (created client-side)

        Returns:
            dict: Result of attachment
            {
                "success": bool,
                "message": str
            }
        """
        try:
            import stripe

            from api.models import Subscription

            # Get subscription to find customer ID
            subscription = self.db.query(Subscription).filter(Subscription.org_id == org_id).first()

            if not subscription:
                return {"success": False, "message": "No subscription found for organization"}

            # Create customer if doesn't exist
            if not subscription.stripe_customer_id:
                customer = stripe.Customer.create(metadata={"org_id": org_id})
                subscription.stripe_customer_id = customer.id
                self.db.commit()
                logger.info(f"Created Stripe customer {customer.id} for org {org_id}")

            # Attach payment method to customer
            stripe.PaymentMethod.attach(payment_method_id, customer=subscription.stripe_customer_id)

            logger.info(
                f"Attached payment method {payment_method_id} to customer "
                f"{subscription.stripe_customer_id} (org {org_id})"
            )

            # If this is the first payment method, set it as default
            customer = stripe.Customer.retrieve(subscription.stripe_customer_id)
            if not customer.invoice_settings.default_payment_method:
                stripe.Customer.modify(
                    subscription.stripe_customer_id,
                    invoice_settings={"default_payment_method": payment_method_id},
                )
                logger.info(f"Set {payment_method_id} as default payment method")

            return {"success": True, "message": "Payment method added successfully"}

        except Exception as e:
            logger.error(f"Error attaching payment method for org {org_id}: {e}")
            return {"success": False, "message": f"Failed to add payment method: {str(e)}"}

    def detach_payment_method(self, org_id: str, payment_method_id: str) -> dict[str, Any]:
        """
        Detach a payment method only from this organization's customer.

        Args:
            payment_method_id: Stripe payment method ID to detach

        Returns:
            dict: Result of detachment
            {
                "success": bool,
                "message": str
            }
        """
        try:
            import stripe
            from sqlalchemy import select

            subscription = self.db.scalar(select(Subscription).where(Subscription.org_id == org_id))
            if not subscription or not subscription.stripe_customer_id:
                return {"success": False, "message": "No Stripe customer found for organization"}
            payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
            if payment_method.get("customer") != subscription.stripe_customer_id:
                return {"success": False, "message": "Payment method not found for organization"}

            # Verify ownership before the provider mutation.
            stripe.PaymentMethod.detach(payment_method_id)

            logger.info(f"Detached payment method {payment_method_id}")

            return {"success": True, "message": "Payment method removed successfully"}

        except Exception as e:
            logger.error(f"Error detaching payment method {payment_method_id}: {e}")
            return {"success": False, "message": f"Failed to remove payment method: {str(e)}"}

    def set_default_payment_method(self, org_id: str, payment_method_id: str) -> dict[str, Any]:
        """
        Set payment method as default for organization's Stripe customer.

        Args:
            org_id: Organization ID
            payment_method_id: Stripe payment method ID to set as default

        Returns:
            dict: Result of update
            {
                "success": bool,
                "message": str
            }
        """
        try:
            import stripe

            from api.models import Subscription

            # Get subscription to find customer ID
            subscription = self.db.query(Subscription).filter(Subscription.org_id == org_id).first()

            if not subscription or not subscription.stripe_customer_id:
                return {"success": False, "message": "No Stripe customer found for organization"}

            payment_method = stripe.PaymentMethod.retrieve(payment_method_id)
            if payment_method.get("customer") != subscription.stripe_customer_id:
                return {"success": False, "message": "Payment method not found for organization"}

            # Set default payment method
            stripe.Customer.modify(
                subscription.stripe_customer_id,
                invoice_settings={"default_payment_method": payment_method_id},
            )

            logger.info(
                f"Set payment method {payment_method_id} as default for "
                f"customer {subscription.stripe_customer_id} (org {org_id})"
            )

            return {"success": True, "message": "Primary payment method updated successfully"}

        except Exception as e:
            logger.error(f"Error setting default payment method for org {org_id}: {e}")
            return {
                "success": False,
                "message": f"Failed to update primary payment method: {str(e)}",
            }

    def create_billing_portal_session(
        self, org_id: str, return_url: str | None = None
    ) -> dict[str, Any]:
        """
        Create Stripe billing portal session for self-service management.

        The billing portal allows customers to:
        - Update payment methods
        - View invoices and payment history
        - Update billing email
        - Cancel subscription (if enabled in Stripe dashboard)

        Args:
            org_id: Organization ID
            return_url: URL to return to after portal session (optional)

        Returns:
            dict: Result with portal URL
            {
                "success": bool,
                "url": str (if successful),
                "message": str
            }
        """
        try:
            import stripe

            from api.models import Subscription

            # Get subscription to find customer ID
            subscription = self.db.query(Subscription).filter(Subscription.org_id == org_id).first()

            if not subscription or not subscription.stripe_customer_id:
                return {
                    "success": False,
                    "message": "No Stripe customer found for organization. Please upgrade to a paid plan first.",
                }

            # Set return URL (default to placeholder if not provided)
            if not return_url:
                return_url = "https://signupflow.io/billing"  # TODO: Use actual app URL

            # Create billing portal session
            session = stripe.billing_portal.Session.create(
                customer=subscription.stripe_customer_id, return_url=return_url
            )

            logger.info(
                f"Created billing portal session for customer {subscription.stripe_customer_id} "
                f"(org {org_id})"
            )

            return {
                "success": True,
                "url": session.url,
                "message": "Billing portal session created successfully",
            }

        except Exception as e:
            logger.error(f"Error creating billing portal session for org {org_id}: {e}")
            return {
                "success": False,
                "message": f"Failed to create billing portal session: {str(e)}",
            }
