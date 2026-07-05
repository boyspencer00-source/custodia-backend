import os
import requests

_SECRET_KEY = None

def _key():
    global _SECRET_KEY
    if _SECRET_KEY is None:
        _SECRET_KEY = os.getenv("FLW_SECRET_KEY")
    return _SECRET_KEY

BASE_URL = "https://api.flutterwave.com/v3"


def _headers():
    return {
        "Authorization": f"Bearer {_key()}",
        "Content-Type": "application/json",
    }


class FlutterwaveError(Exception):
    pass


def _post(path, payload):
    resp = requests.post(f"{BASE_URL}{path}", headers=_headers(), json=payload, timeout=20)
    data = resp.json()
    if data.get("status") != "success":
        raise FlutterwaveError(data.get("message", "Unknown Flutterwave error"))
    return data["data"]


def _get(path, params=None):
    resp = requests.get(f"{BASE_URL}{path}", headers=_headers(), params=params, timeout=20)
    data = resp.json()
    if data.get("status") != "success":
        raise FlutterwaveError(data.get("message", "Unknown Flutterwave error"))
    return data["data"]


# ---------------------------------------------------------------------------
# Virtual account — gives the buyer a unique bank account number to pay into.
# The money lands directly in YOUR Flutterwave merchant balance.
# ---------------------------------------------------------------------------
def create_virtual_account(email: str, amount_naira: float, tx_ref: str,
                            buyer_first: str = "Buyer", buyer_last: str = "User") -> dict:
    """
    Returns {"account_number": "...", "bank_name": "...", "order_ref": "..."}
    The buyer does a regular bank transfer to this account for the exact amount.
    Flutterwave fires a charge.completed webhook when it lands.
    """
    payload = {
        "email":       email,
        "amount":      amount_naira,   # FLW takes naira, not kobo
        "currency":    "NGN",
        "tx_ref":      tx_ref,
        "firstname":   buyer_first,
        "lastname":    buyer_last,
        "narration":   f"Custodia escrow — {tx_ref}",
    }
    return _post("/virtual-account-numbers", payload)


# ---------------------------------------------------------------------------
# Account resolution — verify the seller's account before storing it
# ---------------------------------------------------------------------------
def resolve_account(account_number: str, bank_code: str) -> dict:
    """Returns {"account_name": "...", "account_number": "..."} or raises."""
    return _post("/accounts/resolve", {
        "account_number": account_number,
        "account_bank":   bank_code,
    })


# ---------------------------------------------------------------------------
# Transfer — send money from your FLW balance to the seller's bank account
# ---------------------------------------------------------------------------
def send_to_seller(account_number: str, bank_code: str,
                   amount_naira: float, narration: str, reference: str) -> dict:
    payload = {
        "account_bank":   bank_code,
        "account_number": account_number,
        "amount":         amount_naira,
        "narration":      narration,
        "currency":       "NGN",
        "reference":      reference,
        "debit_currency": "NGN",
    }
    return _post("/transfers", payload)


# ---------------------------------------------------------------------------
# Verify by reference — re-confirm a payment server-side after the webhook
# ---------------------------------------------------------------------------
def verify_by_reference(tx_ref: str) -> dict:
    resp = requests.get(
        f"{BASE_URL}/transactions/verify_by_reference",
        headers=_headers(),
        params={"tx_ref": tx_ref},
        timeout=20,
    )
    data = resp.json()
    if data.get("status") != "success":
        raise FlutterwaveError(data.get("message", "Verification failed"))
    return data["data"]


# ---------------------------------------------------------------------------
# Bank list — for the frontend dropdown
# ---------------------------------------------------------------------------
def list_banks(country: str = "NG") -> list:
    return _get(f"/banks/{country}")
