import stripe as _stripe
from typing import Optional, Tuple


class StripeClient:
    """
    Thin wrapper around Stripe.
    Uses SetupIntent to save a card on file (zero charge upfront).
    Uses off-session PaymentIntent to charge the success fee after a rebook.

    Get your keys at dashboard.stripe.com
    Test card: 4242 4242 4242 4242  exp: any future  cvc: any
    """

    MIN_CHARGE_CENTS = 50  # Stripe minimum

    def __init__(self, secret_key: str):
        _stripe.api_key = secret_key

    def create_customer(self, email: str, name: str):
        return _stripe.Customer.create(email=email, name=name)

    def create_setup_intent(self, customer_id: str):
        return _stripe.SetupIntent.create(
            customer=customer_id,
            payment_method_types=["card"],
        )

    def charge(
        self,
        customer_id: str,
        payment_method_id: str,
        amount_usd: float,
        description: str = "",
    ) -> Optional[object]:
        cents = int(amount_usd * 100)
        if cents < self.MIN_CHARGE_CENTS:
            return None
        return _stripe.PaymentIntent.create(
            amount=cents,
            currency="usd",
            customer=customer_id,
            payment_method=payment_method_id,
            off_session=True,
            confirm=True,
            description=description,
        )

    def charge_success_fee(
        self,
        customer_id: str,
        amount_usd: float,
        booking_id: str,
        net_savings: float,
    ) -> Tuple[bool, Optional[object], Optional[str]]:
        """
        Charge 20% success fee against the agency's stored default card.

        Lookup order for the payment method:
          1. customer.invoice_settings.default_payment_method
          2. customer.default_source
          3. First card returned by PaymentMethod.list

        Returns (success, PaymentIntent_or_None, error_message_or_None).
        """
        cents = int(amount_usd * 100)
        if cents < self.MIN_CHARGE_CENTS:
            return False, None, f"Fee ${amount_usd:.2f} is below Stripe minimum charge"

        try:
            customer = _stripe.Customer.retrieve(customer_id)

            # Resolve the best available payment method
            pm_id = (
                (customer.get("invoice_settings") or {}).get("default_payment_method")
                or customer.get("default_source")
            )
            if not pm_id:
                pms = _stripe.PaymentMethod.list(
                    customer=customer_id, type="card", limit=1
                )
                if pms.data:
                    pm_id = pms.data[0].id

            if not pm_id:
                return False, None, "No payment method on file for this customer"

            description = (
                f"RYDE success fee — booking {booking_id} "
                f"(20% of ${net_savings:.2f} net savings)"
            )
            pi = _stripe.PaymentIntent.create(
                amount=cents,
                currency="usd",
                customer=customer_id,
                payment_method=pm_id,
                off_session=True,
                confirm=True,
                description=description,
                metadata={
                    "booking_id": booking_id,
                    "net_savings": str(round(net_savings, 2)),
                    "fee_pct": "20",
                },
            )
            return True, pi, None

        except _stripe.error.StripeError as exc:
            return False, None, str(exc)
        except Exception as exc:
            return False, None, f"Unexpected billing error: {exc}"
