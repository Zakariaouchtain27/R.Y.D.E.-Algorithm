import stripe as _stripe
from typing import Optional


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
