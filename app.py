"""
Custodia escrow backend — Flutterwave edition

Full transaction flow:
  1. People chat in a room
  2. intent_agent detects a deal → proposes escrow
  3. Buyer confirms "yes" → agent asks seller for bank details
  4. Seller provides account number + bank code → Flutterwave resolves the name
  5. Agent creates a Flutterwave virtual account for the buyer + posts the details
  6. Buyer makes a standard bank transfer to that account
  7. Flutterwave fires charge.completed webhook → money in your balance
     → agent tells both parties; tells seller to ship
  8. Seller ships, confirms in chat
  9. Buyer receives item, sends a PHOTO in chat
 10. ai_agent.review_photo() examines the image with Claude vision
     → approve → auto-transfer (amount − 1%) to seller's verified bank account
     → unclear → ask buyer to resend a clearer photo
     → dispute → freeze for human review
"""

import os
import re
import uuid
import base64
import binascii
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

from db import init_db, get_session
from models import Transaction, Message, MessageType, UserPref, TxStatus, CLOSED_STATES
import flutterwave_client as flw
import ai_agent
import intent_agent
from webhook_verify_flutterwave import is_valid_flutterwave_signature

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
CORS(app)

FEE_RATE       = float(os.getenv("ESCROW_FEE_RATE", "0.01"))  # 1%
CONTEXT_WINDOW = 14
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 MB

URL_RE      = re.compile(r"(https?://\S+|www\.\S+)", re.I)
CONFIRM_RE  = re.compile(r"\b(yes|confirm|ok|okay|sounds good|go ahead|proceed|sure|deal)\b", re.I)
CANCEL_RE   = re.compile(r"\b(no|cancel|stop|never ?mind|back out)\b", re.I)
SHIPPED_RE  = re.compile(r"\b(shipped|sent|dispatched|posted|delivered|on the way|in transit)\b", re.I)

# Bank details pattern: we look for a 10-digit NUBAN + a 3-digit bank code
# The seller is expected to type something like: "0123456789 058"
BANK_DETAILS_RE = re.compile(r"\b(\d{10})\b.*?\b(\d{3})\b")

STICKERS = {
    "thumbs_up": {"emoji": "👍", "label": "Thumbs up"},
    "handshake": {"emoji": "🤝", "label": "Deal"},
    "check":     {"emoji": "✅", "label": "Confirmed"},
    "fire":      {"emoji": "🔥", "label": "Fire"},
    "eyes":      {"emoji": "👀", "label": "Watching"},
    "party":     {"emoji": "🎉", "label": "Celebrate"},
}

init_db()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def msg_dict(m: Message) -> dict:
    return {
        "id":            m.id,
        "room_id":       m.room_id,
        "sender_email":  m.sender_email,
        "is_agent":      m.is_agent,
        "message_type":  m.message_type.value,
        "content":       m.content,
        "image_data":    m.image_data,
        "sticker_id":    m.sticker_id,
        "transaction_id":m.transaction_id,
        "created_at":    m.created_at.isoformat() if m.created_at else None,
    }


def tx_dict(tx: Transaction) -> dict:
    return {
        "id":                    tx.id,
        "room_id":               tx.room_id,
        "buyer_email":           tx.buyer_email,
        "seller_email":          tx.seller_email,
        "item_description":      tx.item_description,
        "amount_kobo":           tx.amount_kobo,
        "fee_kobo":              tx.fee_kobo,
        "seller_account_name":   tx.seller_account_name,
        "virtual_account_number":tx.virtual_account_number,
        "virtual_bank_name":     tx.virtual_bank_name,
        "status":                tx.status.value,
        "created_at":            tx.created_at.isoformat() if tx.created_at else None,
    }


def agent_msg(session, room_id, content, tx_id=None):
    m = Message(room_id=room_id, is_agent=True, message_type=MessageType.TEXT,
                content=content, transaction_id=tx_id)
    session.add(m)
    return m


def active_tx_for(session, room_id, email):
    return (
        session.query(Transaction)
        .filter(
            Transaction.room_id == room_id,
            Transaction.status.notin_(list(CLOSED_STATES)),
            (Transaction.buyer_email == email) | (Transaction.seller_email == email),
        )
        .order_by(Transaction.created_at.desc())
        .first()
    )


def effective_text(m: Message) -> str:
    if m.message_type == MessageType.IMAGE:
        return f"[shared a photo]{(': ' + m.content) if m.content else ''}"
    if m.message_type == MessageType.STICKER:
        return f"[sent a sticker: {STICKERS.get(m.sticker_id, {}).get('label', 'sticker')}]"
    return m.content or ""


# ─────────────────────────────────────────────────────────────────────────────
# Main message route
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/rooms/<room_id>/messages")
def post_message(room_id):
    data         = request.get_json(force=True)
    sender_email = (data.get("sender_email") or "").strip()
    msg_type     = data.get("message_type", "text")
    content      = (data.get("content") or "").strip()
    image_data   = data.get("image_data")
    sticker_id   = data.get("sticker_id")

    if not sender_email:
        return jsonify({"error": "sender_email is required"}), 400
    if msg_type not in ("text", "image", "sticker"):
        return jsonify({"error": "message_type must be text, image, or sticker"}), 400

    is_dm = room_id.startswith("dm:")

    # ── validation ──────────────────────────────────────────────────────────
    if msg_type == "text":
        if not content:
            return jsonify({"error": "content is required"}), 400
        if not is_dm and URL_RE.search(content):
            return jsonify({"error": "Links aren't allowed in public rooms. Use a DM instead."}), 400

    elif msg_type == "image":
        if not image_data:
            return jsonify({"error": "image_data is required"}), 400
        try:
            b64_part = image_data.split(",", 1)[1] if "," in image_data else image_data
            raw = base64.b64decode(b64_part, validate=True)
        except (binascii.Error, ValueError):
            return jsonify({"error": "image_data must be a valid base64 data URL"}), 400
        if len(raw) > MAX_IMAGE_BYTES:
            return jsonify({"error": f"Image too large (max {MAX_IMAGE_BYTES // (1024*1024)} MB)"}), 400

    elif msg_type == "sticker":
        if sticker_id not in STICKERS:
            return jsonify({"error": f"Unknown sticker. Valid ids: {list(STICKERS)}"}), 400

    # ── DM permission ────────────────────────────────────────────────────────
    session = get_session()
    if is_dm:
        ok, err = _dm_permission(session, room_id, sender_email)
        if not ok:
            session.close()
            return jsonify({"error": err}), 403

    # ── persist ──────────────────────────────────────────────────────────────
    incoming = Message(
        room_id=room_id, sender_email=sender_email,
        message_type=MessageType(msg_type),
        content=content or None,
        image_data=image_data if msg_type == "image" else None,
        sticker_id=sticker_id if msg_type == "sticker" else None,
    )
    session.add(incoming)
    session.commit()

    out = [incoming]
    tx  = active_tx_for(session, room_id, sender_email)

    if tx:
        _handle_for_tx(session, tx, sender_email, msg_type, content, image_data, out)
    else:
        _watch_for_deal(session, room_id, out)

    session.commit()
    result = [msg_dict(m) for m in out]
    session.close()
    return jsonify(result), 201


# ─────────────────────────────────────────────────────────────────────────────
# Transaction state machine
# ─────────────────────────────────────────────────────────────────────────────

def _handle_for_tx(session, tx, sender_email, msg_type, content, image_data, out):
    role = "buyer" if sender_email == tx.buyer_email else "seller"
    tid  = tx.id

    # ── PROPOSED: waiting for buyer to say yes ───────────────────────────────
    if tx.status == TxStatus.PROPOSED:
        if role == "seller":
            out.append(agent_msg(session, tx.room_id,
                "Waiting for the buyer to confirm before we proceed.", tid))
            return
        if CONFIRM_RE.search(content or ""):
            tx.status = TxStatus.COLLECTING_SELLER_DETAILS
            out.append(agent_msg(session, tx.room_id,
                f"Great, @{tx.seller_email.split('@')[0]} — to protect the buyer's money I need "
                f"to verify your bank account before issuing payment details.\n\n"
                f"Please reply with your:\n"
                f"• 10-digit account number\n"
                f"• 3-digit bank code (e.g. 058 for GTBank, 011 for First Bank, 033 for UBA, 044 for Access)\n\n"
                f"Example: `0123456789 058`", tid))
        elif CANCEL_RE.search(content or ""):
            tx.status = TxStatus.CANCELLED
            out.append(agent_msg(session, tx.room_id, "Cancelled — no charge was made.", tid))
        else:
            out.append(agent_msg(session, tx.room_id,
                f"To confirm: {tx.item_description} for ₦{tx.amount_kobo/100:,.0f}. "
                f"Reply 'yes' to proceed or 'cancel' to back out.", tid))
        return

    # ── COLLECTING_SELLER_DETAILS: waiting for seller's bank info ────────────
    if tx.status == TxStatus.COLLECTING_SELLER_DETAILS:
        if role == "buyer":
            out.append(agent_msg(session, tx.room_id,
                "Waiting for the seller to provide their bank details — hang tight.", tid))
            return
        # Try to parse account number + bank code from seller's message
        match = BANK_DETAILS_RE.search(content or "")
        if not match:
            out.append(agent_msg(session, tx.room_id,
                "I need your 10-digit account number and 3-digit bank code together, e.g.:\n"
                "`0123456789 058`\n\n"
                "Common codes: GTBank 058 · First Bank 011 · UBA 033 · Access 044 · Zenith 057 · Opay 999992 · Kuda 90267", tid))
            return
        acct, bank_code = match.group(1), match.group(2)
        # Resolve the account name with Flutterwave
        try:
            resolved = flw.resolve_account(acct, bank_code)
        except flw.FlutterwaveError as e:
            out.append(agent_msg(session, tx.room_id,
                f"I couldn't verify that account ({e}). Please double-check the number and bank code and try again.", tid))
            return
        tx.seller_account_number = acct
        tx.seller_bank_code      = bank_code
        tx.seller_account_name   = resolved.get("account_name", "Unknown")
        # Now create the virtual account for the buyer
        reply = _create_virtual_account(session, tx)
        out.append(agent_msg(session, tx.room_id, reply, tid))
        return

    # ── AWAITING_PAYMENT: virtual account given, waiting for transfer ────────
    if tx.status == TxStatus.AWAITING_PAYMENT:
        out.append(agent_msg(session, tx.room_id,
            f"Still waiting for the bank transfer to arrive. "
            f"Please transfer ₦{tx.amount_kobo/100:,.0f} to:\n\n"
            f"Bank: {tx.virtual_bank_name}\n"
            f"Account: {tx.virtual_account_number}\n\n"
            "I'll notify everyone the moment it lands.", tid))
        return

    # ── HOLDING: payment landed, waiting for seller to ship ─────────────────
    if tx.status == TxStatus.HOLDING:
        if role == "seller" and SHIPPED_RE.search(content or ""):
            tx.status = TxStatus.AWAITING_PHOTO
            out.append(agent_msg(session, tx.room_id,
                f"Noted — @{tx.buyer_email.split('@')[0]}, the seller says the item is on its way. "
                f"Once it arrives, **send a photo of the item** in this chat. "
                f"That photo is what releases the payment automatically.", tid))
            return
        # For all other messages, let the chat agent respond
        result = ai_agent.chat(tx, role, content or effective_text_from_type(msg_type))
        _apply_chat_result(session, tx, result, out)
        return

    # ── AWAITING_PHOTO: buyer must send a picture ────────────────────────────
    if tx.status == TxStatus.AWAITING_PHOTO:
        if msg_type == "image":
            # The core moment — Claude reviews the photo
            caption  = content or ""
            decision = ai_agent.review_photo(tx, image_data, caption)

            if decision["decision"] == "approve":
                reply = _execute_payout(session, tx)
                out.append(agent_msg(session, tx.room_id, reply, tid))

            elif decision["decision"] == "unclear":
                out.append(agent_msg(session, tx.room_id,
                    f"I can't clearly identify the item in that photo. "
                    f"{decision['reason']} — please send another photo showing the item.", tid))

            else:  # dispute
                tx.status = TxStatus.DISPUTED
                out.append(agent_msg(session, tx.room_id,
                    f"The photo suggests a problem: {decision['reason']}. "
                    f"I've frozen the funds and flagged this for human review. "
                    f"A team member will be in touch.", tid))
        elif role == "buyer":
            out.append(agent_msg(session, tx.room_id,
                "Please send a **photo** of the received item to release payment. "
                "Text alone isn't enough — tap the camera icon.", tid))
        else:
            out.append(agent_msg(session, tx.room_id,
                "Waiting for the buyer to send a photo of the received item. "
                "Payment releases automatically once they do.", tid))
        return

    # ── DISPUTED / terminal states ───────────────────────────────────────────
    if tx.status == TxStatus.DISPUTED:
        out.append(agent_msg(session, tx.room_id,
            "This transaction is frozen pending human review. No further automated action is possible.", tid))
        return


def effective_text_from_type(msg_type):
    if msg_type == "sticker": return "[sent a sticker]"
    if msg_type == "image":   return "[sent a photo]"
    return ""


def _create_virtual_account(session, tx) -> str:
    ref = f"escrow_{tx.id}_{uuid.uuid4().hex[:8]}"
    try:
        va = flw.create_virtual_account(
            email=tx.buyer_email,
            amount_naira=tx.amount_kobo / 100,
            tx_ref=ref,
        )
    except flw.FlutterwaveError as e:
        return (f"I verified the seller's account (name: {tx.seller_account_name}) "
                f"but couldn't generate a payment account right now ({e}). Please try again.")

    tx.virtual_account_number = va.get("account_number")
    tx.virtual_bank_name      = va.get("bank_name")
    tx.payment_reference      = ref
    tx.status                 = TxStatus.AWAITING_PAYMENT

    return (
        f"✅ Seller verified: **{tx.seller_account_name}**\n\n"
        f"@{tx.buyer_email.split('@')[0]}, please make a bank transfer of exactly "
        f"**₦{tx.amount_kobo/100:,.0f}** to:\n\n"
        f"🏦 Bank: **{tx.virtual_bank_name}**\n"
        f"📋 Account: **{tx.virtual_account_number}**\n\n"
        f"The money goes directly to Custodia escrow. I'll notify everyone the moment it arrives."
    )


def _execute_payout(session, tx) -> str:
    session.refresh(tx)
    if tx.status not in (TxStatus.AWAITING_PHOTO,):
        return f"Transaction is already '{tx.status.value}'."

    fee_kobo    = round(tx.amount_kobo * FEE_RATE)
    payout_kobo = tx.amount_kobo - fee_kobo
    reference   = f"payout_{tx.id}_{uuid.uuid4().hex[:8]}"

    try:
        transfer = flw.send_to_seller(
            account_number=tx.seller_account_number,
            bank_code=tx.seller_bank_code,
            amount_naira=payout_kobo / 100,
            narration=f"Custodia escrow payout — {tx.item_description[:40]}",
            reference=reference,
        )
    except flw.FlutterwaveError as e:
        return (f"The buyer's photo was approved but the transfer failed: {e}. "
                f"Our team will complete the payout manually — sorry for the delay.")

    tx.status          = TxStatus.RELEASED
    tx.fee_kobo        = fee_kobo
    tx.payout_reference= transfer.get("reference", reference)
    tx.resolved_at     = datetime.utcnow()

    return (
        f"🎉 Photo confirmed — item received!\n\n"
        f"**₦{payout_kobo/100:,.0f}** is on its way to {tx.seller_account_name} "
        f"({tx.seller_bank_code} · {tx.seller_account_number}).\n"
        f"Transfer ref: `{tx.payout_reference}`\n\n"
        f"_(₦{fee_kobo/100:,.0f} escrow fee retained — 1% of deal value)_"
    )


def _apply_chat_result(session, tx, result, out):
    reply  = result["reply"]
    action = result["action"]
    reason = result["reason"]
    if action == "dispute":
        tx.status = TxStatus.DISPUTED
        reply = reply or f"Flagged for human review ({reason}). Funds are frozen."
    out.append(agent_msg(session, tx.room_id, reply or "Got it.", tx.id))


# ─────────────────────────────────────────────────────────────────────────────
# Intent detection — watches the room for deals
# ─────────────────────────────────────────────────────────────────────────────

def _watch_for_deal(session, room_id, out):
    recent = (
        session.query(Message)
        .filter(Message.room_id == room_id)
        .order_by(Message.created_at.desc())
        .limit(CONTEXT_WINDOW)
        .all()
    )
    transcript = [
        {"sender": "agent" if m.is_agent else m.sender_email, "content": effective_text(m)}
        for m in reversed(recent)
    ]

    verdict = intent_agent.evaluate_conversation(transcript)
    if not verdict.get("ready"):
        return

    buyer_email  = verdict.get("buyer_email")
    seller_email = verdict.get("seller_email")
    item         = verdict.get("item_description")
    amount_naira = verdict.get("amount_naira")

    if not (buyer_email and seller_email and item and amount_naira): return
    if buyer_email == seller_email: return
    if active_tx_for(session, room_id, buyer_email): return
    if active_tx_for(session, room_id, seller_email): return

    amount_kobo = round(float(amount_naira) * 100)
    tx = Transaction(
        room_id=room_id, buyer_email=buyer_email, seller_email=seller_email,
        item_description=item, amount_kobo=amount_kobo,
        status=TxStatus.PROPOSED,
        detected_from_message_id=out[-1].id if out else None,
    )
    session.add(tx)
    session.flush()

    out.append(agent_msg(session, room_id,
        f"I spotted a deal: **{item}** for **₦{amount_kobo/100:,.0f}** between "
        f"@{buyer_email.split('@')[0]} (buyer) and @{seller_email.split('@')[0]} (seller).\n\n"
        f"@{buyer_email.split('@')[0]}, reply **'yes'** to open escrow. Here's how it works:\n"
        f"• I'll collect the seller's bank details and verify them\n"
        f"• You'll get a dedicated account number to pay into\n"
        f"• Money sits safely in escrow until you confirm receipt with a photo\n"
        f"• Payment goes to the seller automatically — no manual steps", tx.id))


# ─────────────────────────────────────────────────────────────────────────────
# DM permission
# ─────────────────────────────────────────────────────────────────────────────

def _dm_room_parts(room_id):
    body  = room_id[3:]
    parts = body.split("::")
    return (parts[0], parts[1]) if len(parts) == 2 else None


def _dm_permission(session, room_id, sender_email):
    parts = _dm_room_parts(room_id)
    if not parts:
        return False, "Malformed DM room id."
    if sender_email.lower() not in parts:
        return False, "You're not a participant in this conversation."
    already_started = session.query(Message).filter(Message.room_id == room_id).first() is not None
    if already_started:
        return True, None
    other = parts[0] if parts[1] == sender_email.lower() else parts[1]
    pref  = session.get(UserPref, other)
    if pref and pref.accepts_open_dms:
        return True, None
    return False, f"{other} hasn't turned on open DMs."


# ─────────────────────────────────────────────────────────────────────────────
# Other routes
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/rooms/<room_id>/messages")
def get_messages(room_id):
    since_id = request.args.get("since_id", type=int)
    session  = get_session()
    q        = session.query(Message).filter(Message.room_id == room_id)
    if since_id:
        q = q.filter(Message.id > since_id)
    msgs   = q.order_by(Message.created_at.asc()).all()
    result = [msg_dict(m) for m in msgs]
    session.close()
    return jsonify(result), 200


@app.get("/stickers")
def get_stickers():
    return jsonify(STICKERS), 200


@app.get("/banks")
def get_banks():
    try:
        return jsonify(flw.list_banks()), 200
    except flw.FlutterwaveError as e:
        return jsonify({"error": str(e)}), 502


@app.get("/users/<email>/prefs")
def get_prefs(email):
    session = get_session()
    pref    = session.get(UserPref, email)
    result  = {"email": email, "accepts_open_dms": bool(pref and pref.accepts_open_dms)}
    session.close()
    return jsonify(result), 200


@app.post("/users/<email>/prefs")
def set_prefs(email):
    data    = request.get_json(force=True)
    accepts = bool(data.get("accepts_open_dms"))
    session = get_session()
    pref    = session.get(UserPref, email)
    if pref:
        pref.accepts_open_dms = accepts
    else:
        session.add(UserPref(email=email, accepts_open_dms=accepts))
    session.commit()
    session.close()
    return jsonify({"email": email, "accepts_open_dms": accepts}), 200


@app.post("/dms/start")
def start_dm():
    data = request.get_json(force=True)
    a, b = data.get("email_a", ""), data.get("email_b", "")
    if not (a and b) or a.lower() == b.lower():
        return jsonify({"error": "email_a and email_b are required and must differ"}), 400
    room_id = "dm:" + "::".join(sorted([a.lower(), b.lower()]))
    session = get_session()
    ok, err = _dm_permission(session, room_id, a.lower())
    session.close()
    if not ok:
        return jsonify({"error": err}), 403
    return jsonify({"room_id": room_id}), 200


@app.get("/transactions/<tx_id>")
def get_transaction(tx_id):
    session = get_session()
    tx      = session.get(Transaction, tx_id)
    if not tx:
        session.close()
        return jsonify({"error": "Not found"}), 404
    result = tx_dict(tx)
    session.close()
    return jsonify(result), 200


# ─────────────────────────────────────────────────────────────────────────────
# Flutterwave webhook
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/webhooks/flutterwave")
def flutterwave_webhook():
    raw_body = request.get_data()
    if not is_valid_flutterwave_signature(raw_body, request.headers):
        return jsonify({"error": "Invalid signature"}), 401

    event = request.get_json(force=True)
    if event.get("event") not in ("charge.completed", "transfer.completed"):
        return jsonify({"ok": True}), 200

    tx_ref = event.get("data", {}).get("tx_ref") or event.get("data", {}).get("reference")
    if not tx_ref:
        return jsonify({"ok": True}), 200

    # Re-verify server-side — never trust the webhook payload alone
    try:
        verified = flw.verify_by_reference(tx_ref)
    except flw.FlutterwaveError:
        return jsonify({"error": "Could not verify transaction"}), 502

    if verified.get("status") != "successful":
        return jsonify({"ok": True}), 200

    session = get_session()
    tx = session.query(Transaction).filter_by(payment_reference=tx_ref).first()
    if tx and tx.status == TxStatus.AWAITING_PAYMENT:
        tx.status    = TxStatus.HOLDING
        tx.funded_at = datetime.utcnow()
        agent_msg(session, tx.room_id,
            f"💰 Payment received! ₦{tx.amount_kobo/100:,.0f} is now held in escrow.\n\n"
            f"@{tx.seller_email.split('@')[0]} — you can ship the item now. "
            f"Once you've sent it, confirm here so the buyer knows to expect it.\n\n"
            f"@{tx.buyer_email.split('@')[0]} — when your item arrives, "
            f"send a photo of it here to release payment to the seller.", tx.id)
        session.commit()
    session.close()
    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
