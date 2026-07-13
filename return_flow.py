"""
return_flow.py
All logic for the unsatisfied-buyer return journey.
app.py calls into this module so the main file stays readable.

State machine:
  AWAITING_PHOTO
      │  buyer types "not satisfied…"
      ▼
  RETURN_REQUESTED          ← 48hr address deadline starts
      │  buyer provides address within 48hr
      ▼
  RETURN_ADDRESS_PROVIDED   ← seller picks window [24h/48h/7d/14d/30d]
      │  seller picks
      ▼
  RETURN_IN_TRANSIT         ← return_deadline starts; secret code generated
      │  seller arrives, sends photo of returned product
      ▼
  RETURN_RECEIVED           ← seller gives buyer secret code verbally/physically
      │  buyer types the secret code in DM
      ▼
  REFUNDED                  ← auto-transfer back to buyer bank account

Countdowns are enforced by the scheduler (scheduler.py).
"""

import os
import re
import uuid
import secrets
from datetime import datetime, timedelta

import flutterwave_client as flw
import ai_agent

# Regex to detect "not satisfied" trigger
DISSATISFIED_RE = re.compile(
    r"\b(not satisfied|unsatisfied|not happy|unhappy|damaged|wrong item"
    r"|not what i ordered|broken|bad condition|fake|scam)\b",
    re.I,
)

# Regex to detect 10-digit account + 3-digit bank code for buyer refund details
BANK_RE = re.compile(r"\b(\d{10})\b.*?\b(\d{3})\b")

# Window options seller can pick
WINDOW_OPTIONS = {
    "24":   24,
    "48":   48,
    "7":    7   * 24,
    "14":   14  * 24,
    "30":   30  * 24,
}
WINDOW_LABEL = {
    24:    "24 hours",
    48:    "48 hours",
    168:   "7 days",
    336:   "14 days",
    720:   "1 month",
}

FEE_RATE = float(os.getenv("ESCROW_FEE_RATE", "0.01"))


def is_dissatisfied(text: str) -> bool:
    return bool(DISSATISFIED_RE.search(text or ""))


def gen_dispute_room_id(tx_id: str) -> str:
    return f"dispute:{tx_id}"


def gen_secret() -> str:
    return secrets.token_hex(3).upper()   # e.g. "A3F9C2"


# ── Step 1: buyer says not satisfied ─────────────────────────────────────────
def handle_dissatisfied(session, tx, agent_msg_fn) -> list:
    """
    Called when the buyer's message triggers is_dissatisfied().
    Creates the dispute DM room, sets address deadline, transitions status.
    Returns list of new agent Message objects (already added to session).
    """
    from models import TxStatus, DmRoom
    out = []

    # Create the 3-way dispute DM
    room_id = gen_dispute_room_id(tx.id)
    existing = session.get(DmRoom, room_id)
    if not existing:
        session.add(DmRoom(room_id=room_id, is_dispute_room=True))

    tx.dispute_room_id  = room_id
    tx.status           = TxStatus.RETURN_REQUESTED
    tx.address_deadline = datetime.utcnow() + timedelta(hours=48)

    # Message in the ORIGINAL room
    out.append(agent_msg_fn(
        session, tx.room_id,
        f"I've noted your concern, @{tx.buyer_email.split('@')[0]}. "
        f"I've opened a private resolution thread for this transaction — "
        f"check your DMs (room ID: `{room_id}`).",
        tx.id,
    ))

    # Opening message in the DISPUTE room
    out.append(agent_msg_fn(
        session, room_id,
        f"This is a private resolution thread for your transaction: "
        f"**{tx.item_description}** · ₦{tx.amount_kobo/100:,.0f}\n\n"
        f"@{tx.buyer_email.split('@')[0]}, the seller's funds are frozen.\n\n"
        f"To start the return process, please provide your **full delivery address** "
        f"(street, city, state) so the seller can come to collect the item.\n\n"
        f"⏳ You have **48 hours** to provide your address. If no address is received "
        f"by then, the funds will be released to the seller automatically.",
        tx.id,
    ))
    return out


# ── Step 2: buyer provides address ───────────────────────────────────────────
def handle_address_provided(session, tx, text: str, agent_msg_fn) -> list:
    """
    Buyer sends their address in the dispute room.
    We don't validate the format strictly — any multi-word reply is treated as an address.
    """
    from models import TxStatus
    out = []

    if len(text.split()) < 3:
        out.append(agent_msg_fn(session, tx.dispute_room_id,
            "Please provide your full address including street, city, and state.", tx.id))
        return out

    tx.buyer_return_address = text
    tx.status = TxStatus.RETURN_ADDRESS_PROVIDED

    out.append(agent_msg_fn(session, tx.dispute_room_id,
        f"✅ Address received: **{text}**\n\n"
        f"@{tx.seller_email.split('@')[0]}, please choose your return window — "
        f"how long do you need to travel to the buyer's location and collect the item?\n\n"
        f"Reply with one of:\n"
        f"• **24** — 24 hours\n"
        f"• **48** — 48 hours\n"
        f"• **7** — 7 days\n"
        f"• **14** — 14 days\n"
        f"• **30** — 1 month",
        tx.id,
    ))
    return out


# ── Step 3: seller picks a window ────────────────────────────────────────────
def handle_window_pick(session, tx, text: str, agent_msg_fn, session_get_or_create_dm) -> list:
    """
    Seller picks their return window. We create a SELLER-ONLY DM for the secret code.
    """
    from models import TxStatus, DmRoom
    out = []

    # Parse the number from the seller's reply
    match = re.search(r"\b(24|48|7|14|30)\b", text)
    if not match:
        out.append(agent_msg_fn(session, tx.dispute_room_id,
            "Please reply with just the number: 24, 48, 7, 14, or 30.", tx.id))
        return out

    hours = WINDOW_OPTIONS[match.group(1)]
    label = WINDOW_LABEL[hours]

    # Generate secret code + set deadline
    code = gen_secret()
    tx.return_secret_code  = code
    tx.return_window_hours = hours
    tx.return_deadline     = datetime.utcnow() + timedelta(hours=hours)
    tx.status              = TxStatus.RETURN_IN_TRANSIT

    # Message to the dispute room (buyer can see this)
    out.append(agent_msg_fn(session, tx.dispute_room_id,
        f"@{tx.seller_email.split('@')[0]} has chosen **{label}** to return to the buyer's location.\n\n"
        f"📍 Buyer's address: **{tx.buyer_return_address}**\n\n"
        f"⏳ Deadline: **{tx.return_deadline.strftime('%d %b %Y, %H:%M UTC')}**\n\n"
        f"@{tx.seller_email.split('@')[0]}, once you arrive and the buyer hands back the item, "
        f"**take a photo of the returned product** and send it here to stop the countdown. "
        f"The buyer will then receive a code to type here to confirm and get their refund.\n\n"
        f"If the deadline passes without a photo, the buyer is automatically refunded.",
        tx.id,
    ))

    # Secret code — send ONLY to seller via a private seller-only message
    # We do this by posting a message in the dispute room flagged visible_to = seller only
    # Since we don't have per-user visibility in the DB, we use a separate room
    seller_secret_room = f"secret:{tx.id}"
    existing = session.get(DmRoom, seller_secret_room)
    if not existing:
        session.add(DmRoom(room_id=seller_secret_room, is_dispute_room=True))

    out.append(agent_msg_fn(session, seller_secret_room,
        f"🔐 YOUR SECRET CODE (DO NOT SHARE WITH THE BUYER YET):\n\n"
        f"**{code}**\n\n"
        f"Once you have collected the returned item and sent a photo in the dispute room, "
        f"give this code to the buyer in person. They will type it to confirm receipt of "
        f"the refund — only then will money move back to them.",
        tx.id,
    ))

    return out


# ── Step 4: seller sends photo of returned product ───────────────────────────
def handle_return_photo(session, tx, image_data: str, caption: str, agent_msg_fn) -> list:
    """
    Seller sends a photo of the item they collected back from the buyer.
    AI reviews the photo to confirm it shows a product.
    """
    from models import TxStatus
    out = []

    decision = ai_agent.review_return_photo(tx, image_data, caption)

    if decision["decision"] != "approve":
        out.append(agent_msg_fn(session, tx.dispute_room_id,
            f"I can't clearly confirm the returned item in that photo. "
            f"{decision['reason']} — please resend a clear photo of the product.",
            tx.id,
        ))
        return out

    # Photo approved — stop the countdown, tell seller to share code with buyer
    tx.status = TxStatus.RETURN_RECEIVED

    out.append(agent_msg_fn(session, tx.dispute_room_id,
        f"✅ Return photo confirmed — the countdown is now stopped.\n\n"
        f"@{tx.seller_email.split('@')[0]}, give the buyer the secret code you received "
        f"in your private messages.\n\n"
        f"@{tx.buyer_email.split('@')[0]}, once the seller gives you the code, "
        f"type it here to receive your refund. Also please provide your bank details:\n"
        f"• 10-digit account number\n"
        f"• 3-digit bank code\n"
        f"Example: `0123456789 058`",
        tx.id,
    ))
    return out


# ── Step 5: buyer types secret code + provides bank details ──────────────────
def handle_secret_code_and_refund(session, tx, text: str, agent_msg_fn) -> list:
    """
    Buyer types the secret code. We also look for bank details in the same or
    subsequent messages. Once both are present, fire the refund.
    """
    from models import TxStatus
    out = []

    # Check for secret code
    code_match = re.search(r"\b([A-F0-9]{6})\b", text.upper())
    if code_match:
        typed_code = code_match.group(1)
        if typed_code != tx.return_secret_code:
            out.append(agent_msg_fn(session, tx.dispute_room_id,
                "That code doesn't match. Please check with the seller and try again.", tx.id))
            return out

    # Check for bank details
    bank_match = BANK_RE.search(text)
    if bank_match:
        acct, bank_code = bank_match.group(1), bank_match.group(2)
        try:
            resolved = flw.resolve_account(acct, bank_code)
            tx.buyer_account_number = acct
            tx.buyer_bank_code      = bank_code
            tx.buyer_account_name   = resolved.get("account_name", "Unknown")
        except flw.FlutterwaveError as e:
            out.append(agent_msg_fn(session, tx.dispute_room_id,
                f"Couldn't verify that bank account ({e}). Please check and try again.", tx.id))
            return out

    # If we have a verified code AND bank details, fire the refund
    code_ok  = code_match and code_match.group(1) == tx.return_secret_code
    bank_ok  = bool(tx.buyer_account_number and tx.buyer_bank_code)

    if not code_ok:
        out.append(agent_msg_fn(session, tx.dispute_room_id,
            "Please type the 6-character secret code the seller gave you.", tx.id))
        return out

    if not bank_ok:
        out.append(agent_msg_fn(session, tx.dispute_room_id,
            "✅ Code accepted! Now please provide your bank details to receive the refund:\n"
            "• 10-digit account number + 3-digit bank code\n"
            "Example: `0123456789 058`", tx.id))
        return out

    # Both present — execute the refund
    result = _execute_refund(session, tx)
    out.append(agent_msg_fn(session, tx.dispute_room_id, result, tx.id))
    return out


# ── Shared refund executor ────────────────────────────────────────────────────
def _execute_refund(session, tx) -> str:
    """Transfer full amount back to buyer. No fee on a refund."""
    from models import TxStatus
    session.refresh(tx)

    reference = f"refund_{tx.id}_{uuid.uuid4().hex[:8]}"
    try:
        transfer = flw.send_to_seller(   # reuse same FLW transfer endpoint
            account_number=tx.buyer_account_number,
            bank_code=tx.buyer_bank_code,
            amount_naira=tx.amount_kobo / 100,
            narration=f"Custodia refund — {tx.item_description[:40]}",
            reference=reference,
        )
    except flw.FlutterwaveError as e:
        return (f"Refund initiation failed: {e}. Our team will process this manually "
                f"within 24 hours — you will be contacted.")

    tx.status          = TxStatus.REFUNDED
    tx.payout_reference= transfer.get("reference", reference)
    tx.resolved_at     = datetime.utcnow()

    return (
        f"🎉 Refund processed!\n\n"
        f"₦{tx.amount_kobo/100:,.0f} is on its way to "
        f"{tx.buyer_account_name} ({tx.buyer_bank_code} · {tx.buyer_account_number}).\n"
        f"Reference: `{tx.payout_reference}`\n\n"
        f"This transaction is now closed."
    )


# ── Auto-actions called by the scheduler ─────────────────────────────────────
def auto_release_for_no_address(session, tx, agent_msg_fn) -> str:
    """Called by scheduler when address_deadline passes without buyer address."""
    from models import TxStatus
    import flutterwave_client as flw2
    import uuid as _uuid

    fee_kobo    = round(tx.amount_kobo * FEE_RATE)
    payout_kobo = tx.amount_kobo - fee_kobo
    reference   = f"payout_{tx.id}_{_uuid.uuid4().hex[:8]}"

    try:
        transfer = flw2.send_to_seller(
            account_number=tx.seller_account_number,
            bank_code=tx.seller_bank_code,
            amount_naira=payout_kobo / 100,
            narration=f"Custodia auto-release — buyer address not provided",
            reference=reference,
        )
        tx.status           = TxStatus.RELEASED
        tx.fee_kobo         = fee_kobo
        tx.payout_reference = transfer.get("reference", reference)
        tx.resolved_at      = datetime.utcnow()
        msg = (f"⏰ 48 hours have passed without a delivery address from the buyer.\n"
               f"₦{payout_kobo/100:,.0f} has been automatically released to the seller.\n"
               f"Transaction closed.")
    except Exception as e:
        msg = f"Auto-release failed: {e}. Manual review required."

    agent_msg_fn(session, tx.dispute_room_id or tx.room_id, msg, tx.id)
    session.commit()


def auto_refund_for_expired_return(session, tx, agent_msg_fn) -> str:
    """Called by scheduler when return_deadline passes without seller photo."""
    from models import TxStatus

    if not (tx.buyer_account_number and tx.buyer_bank_code):
        # We don't have buyer bank details yet — ask in the dispute room
        tx.status = TxStatus.RETURN_RECEIVED  # keep open for bank detail collection
        agent_msg_fn(session, tx.dispute_room_id,
            f"⏰ The seller's return deadline has passed without a confirmed delivery.\n\n"
            f"@{tx.buyer_email.split('@')[0]}, you are entitled to a full refund of "
            f"₦{tx.amount_kobo/100:,.0f}.\n\n"
            f"Please provide your bank details to receive it:\n"
            f"• 10-digit account number + 3-digit bank code\n"
            f"Example: `0123456789 058`",
            tx.id,
        )
        session.commit()
        return

    result = _execute_refund(session, tx)
    agent_msg_fn(session, tx.dispute_room_id, result, tx.id)
    session.commit()
