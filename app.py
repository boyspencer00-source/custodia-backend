"""
Custodia escrow backend — full feature set:
  • Global chat with AI deal detection
  • Virtual Flutterwave account for buyer payment
  • Photo-gated release to seller (minus 1%)
  • Full return flow when buyer is not satisfied
  • Opt-in DMs (no links allowed in DMs either)
  • Emoji-only mode for DMs (not available in dispute rooms)
  • Dispute rooms created automatically on return request
"""

import os, re, uuid, base64, binascii, unicodedata
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
load_dotenv()

from db import init_db, get_session
from models import (Transaction, Message, MessageType, UserPref,
                    TxStatus, CLOSED_STATES, DmRoom, DmRoomMode)
import flutterwave_client as flw
import ai_agent, intent_agent, return_flow
from webhook_verify_flutterwave import is_valid_flutterwave_signature

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
CORS(app)

FEE_RATE        = float(os.getenv("ESCROW_FEE_RATE", "0.01"))
CONTEXT_WINDOW  = 14
MAX_IMAGE_BYTES = 4 * 1024 * 1024
URL_RE          = re.compile(r"(https?://\S+|www\.\S+)", re.I)
CONFIRM_RE      = re.compile(r"\b(yes|confirm|ok|okay|sounds good|go ahead|proceed|sure|deal)\b", re.I)
CANCEL_RE       = re.compile(r"\b(no|cancel|stop|never ?mind|back out)\b", re.I)
SHIPPED_RE      = re.compile(r"\b(shipped|sent|dispatched|posted|delivered|on the way|in transit)\b", re.I)
BANK_RE         = re.compile(r"\b(\d{10})\b.*?\b(\d{3})\b")
WINDOW_RE       = re.compile(r"\b(24|48|7|14|30)\b")

STICKERS = {
    "thumbs_up":{"emoji":"👍","label":"Thumbs up"},
    "handshake": {"emoji":"🤝","label":"Deal"},
    "check":     {"emoji":"✅","label":"Confirmed"},
    "fire":      {"emoji":"🔥","label":"Fire"},
    "eyes":      {"emoji":"👀","label":"Watching"},
    "party":     {"emoji":"🎉","label":"Celebrate"},
}

init_db()

# Start the deadline scheduler
from scheduler import start_scheduler
start_scheduler(app)


# ── helpers ───────────────────────────────────────────────────────────────────
def msg_dict(m):
    return {
        "id": m.id, "room_id": m.room_id, "sender_email": m.sender_email,
        "is_agent": m.is_agent, "message_type": m.message_type.value,
        "content": m.content, "image_data": m.image_data, "sticker_id": m.sticker_id,
        "transaction_id": m.transaction_id,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }

def tx_dict(tx):
    return {
        "id": tx.id, "room_id": tx.room_id,
        "buyer_email": tx.buyer_email, "seller_email": tx.seller_email,
        "item_description": tx.item_description,
        "amount_kobo": tx.amount_kobo, "fee_kobo": tx.fee_kobo,
        "seller_account_name": tx.seller_account_name,
        "virtual_account_number": tx.virtual_account_number,
        "virtual_bank_name": tx.virtual_bank_name,
        "status": tx.status.value,
        "dispute_room_id": tx.dispute_room_id,
        "return_deadline": tx.return_deadline.isoformat() if tx.return_deadline else None,
        "address_deadline": tx.address_deadline.isoformat() if tx.address_deadline else None,
        "created_at": tx.created_at.isoformat() if tx.created_at else None,
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

def tx_for_dispute_room(session, room_id):
    """Find the transaction whose dispute_room_id matches this room."""
    return (
        session.query(Transaction)
        .filter(Transaction.dispute_room_id == room_id)
        .filter(Transaction.status.notin_(list(CLOSED_STATES)))
        .first()
    )

def tx_for_secret_room(session, room_id):
    """Find the transaction for a seller secret room 'secret:<tx_id>'."""
    if not room_id.startswith("secret:"):
        return None
    tx_id = room_id[7:]
    return session.get(Transaction, tx_id)

def effective_text(m):
    if m.message_type == MessageType.IMAGE:
        return f"[shared a photo]{(': ' + m.content) if m.content else ''}"
    if m.message_type == MessageType.STICKER:
        return f"[sent a sticker: {STICKERS.get(m.sticker_id, {}).get('label', 'sticker')}]"
    return m.content or ""

def is_emoji_only(text: str) -> bool:
    """Return True if every non-whitespace character in text is an emoji."""
    for ch in text:
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        # Emoji characters typically fall in So (Symbol, other) or have high codepoints
        cp = ord(ch)
        if not (cat == "So" or 0x1F300 <= cp <= 0x1FBFF or
                0x2600 <= cp <= 0x27BF or 0xFE00 <= cp <= 0xFE0F or
                0x1F1E0 <= cp <= 0x1F1FF):
            return False
    return len(text.strip()) > 0


# ── DM helpers ────────────────────────────────────────────────────────────────
def dm_room_parts(room_id):
    body = room_id[3:]
    parts = body.split("::")
    return (parts[0], parts[1]) if len(parts) == 2 else None

def dm_permission(session, room_id, sender_email):
    parts = dm_room_parts(room_id)
    if not parts:
        return False, "Malformed DM room id."
    if sender_email.lower() not in parts:
        return False, "You're not a participant in this conversation."
    already = session.query(Message).filter(Message.room_id == room_id).first()
    if already:
        return True, None
    other = parts[0] if parts[1] == sender_email.lower() else parts[1]
    pref  = session.get(UserPref, other)
    if pref and pref.accepts_open_dms:
        return True, None
    return False, f"{other} hasn't turned on open DMs."

def get_or_create_dm_room(session, room_id, is_dispute=False):
    room = session.get(DmRoom, room_id)
    if not room:
        room = DmRoom(room_id=room_id, is_dispute_room=is_dispute)
        session.add(room)
    return room


# ── main message route ────────────────────────────────────────────────────────
@app.post("/rooms/<room_id>/messages")
def post_message(room_id):
    data         = request.get_json(force=True)
    sender_email = (data.get("sender_email") or "").strip().lower()
    msg_type     = data.get("message_type", "text")
    content      = (data.get("content") or "").strip()
    image_data   = data.get("image_data")
    sticker_id   = data.get("sticker_id")

    if not sender_email:
        return jsonify({"error": "sender_email is required"}), 400
    if msg_type not in ("text", "image", "sticker"):
        return jsonify({"error": "message_type must be text, image, or sticker"}), 400

    is_global   = room_id == "global"
    is_dm       = room_id.startswith("dm:")
    is_dispute  = room_id.startswith("dispute:")
    is_secret   = room_id.startswith("secret:")

    session = get_session()

    # ── Emoji-only mode validation ────────────────────────────────────
    if is_dm and not is_dispute and msg_type == "text":
        dm_room = session.get(DmRoom, room_id)
        if dm_room and dm_room.mode == DmRoomMode.EMOJI_ONLY and not dm_room.is_dispute_room:
            if not is_emoji_only(content):
                session.close()
                return jsonify({"error": "This chat is in emoji-only mode. Send emojis only — no words, links, or numbers."}), 400

    # ── Link blocking (global room AND DMs) ──────────────────────────
    if msg_type == "text" and (is_global or is_dm or is_dispute):
        if URL_RE.search(content):
            session.close()
            return jsonify({"error": "Links aren't allowed here."}), 400

    # ── Image validation ─────────────────────────────────────────────
    if msg_type == "image":
        if not image_data:
            session.close()
            return jsonify({"error": "image_data is required"}), 400
        try:
            b64 = image_data.split(",", 1)[1] if "," in image_data else image_data
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            session.close()
            return jsonify({"error": "image_data must be valid base64"}), 400
        if len(raw) > MAX_IMAGE_BYTES:
            session.close()
            return jsonify({"error": f"Image too large (max {MAX_IMAGE_BYTES//(1024*1024)}MB)"}), 400

    # ── Sticker validation ───────────────────────────────────────────
    if msg_type == "sticker" and sticker_id not in STICKERS:
        session.close()
        return jsonify({"error": f"Unknown sticker_id. Options: {list(STICKERS)}"}), 400

    # ── DM permission ────────────────────────────────────────────────
    if is_dm:
        ok, err = dm_permission(session, room_id, sender_email)
        if not ok:
            session.close()
            return jsonify({"error": err}), 403

    # ── Persist ──────────────────────────────────────────────────────
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

    # ── Route to correct handler ──────────────────────────────────────
    if is_dispute:
        _handle_dispute_room(session, room_id, sender_email, msg_type,
                             content, image_data, out)
    elif is_secret:
        # Secret room is read-only for the seller — agent posts here, seller reads
        pass
    else:
        tx = active_tx_for(session, room_id, sender_email)
        if tx:
            _handle_for_tx(session, tx, sender_email, msg_type, content, image_data, out)
        else:
            _watch_for_deal(session, room_id, out)

    session.commit()
    result = [msg_dict(m) for m in out]
    session.close()
    return jsonify(result), 201


# ── dispute room handler ──────────────────────────────────────────────────────
def _handle_dispute_room(session, room_id, sender_email, msg_type, content, image_data, out):
    tx = tx_for_dispute_room(session, room_id)
    if not tx:
        out.append(agent_msg(session, room_id, "No active transaction found for this room."))
        return

    role = "buyer" if sender_email == tx.buyer_email else "seller"

    # RETURN_REQUESTED: waiting for buyer's address
    if tx.status == TxStatus.RETURN_REQUESTED:
        if role == "buyer" and msg_type == "text" and content:
            msgs = return_flow.handle_address_provided(session, tx, content, agent_msg)
            out.extend(msgs)
        elif role == "seller":
            out.append(agent_msg(session, room_id,
                "Please wait — we're collecting the buyer's address first.", tx.id))
        return

    # RETURN_ADDRESS_PROVIDED: waiting for seller to pick window
    if tx.status == TxStatus.RETURN_ADDRESS_PROVIDED:
        if role == "seller" and msg_type == "text":
            msgs = return_flow.handle_window_pick(
                session, tx, content, agent_msg, get_or_create_dm_room)
            out.extend(msgs)
        elif role == "buyer":
            out.append(agent_msg(session, room_id,
                "Waiting for the seller to choose their return window.", tx.id))
        return

    # RETURN_IN_TRANSIT: countdown running — only seller photo stops it
    if tx.status == TxStatus.RETURN_IN_TRANSIT:
        if role == "seller" and msg_type == "image":
            msgs = return_flow.handle_return_photo(
                session, tx, image_data, content or "", agent_msg)
            out.extend(msgs)
        elif role == "seller" and msg_type == "text":
            deadline_str = tx.return_deadline.strftime("%d %b %Y %H:%M UTC") if tx.return_deadline else "soon"
            out.append(agent_msg(session, room_id,
                f"Send a **photo** of the returned item to stop the countdown.\n"
                f"Deadline: {deadline_str}", tx.id))
        elif role == "buyer":
            out.append(agent_msg(session, room_id,
                "The seller is on their way. The countdown is running — "
                "you will receive a refund automatically if they don't make it in time.", tx.id))
        return

    # RETURN_RECEIVED: buyer needs to give code + bank details
    if tx.status == TxStatus.RETURN_RECEIVED:
        if role == "buyer" and msg_type == "text":
            msgs = return_flow.handle_secret_code_and_refund(session, tx, content, agent_msg)
            out.extend(msgs)
        elif role == "seller":
            out.append(agent_msg(session, room_id,
                "Give the buyer the secret code from your private messages so "
                "they can confirm and receive their refund.", tx.id))
        return

    out.append(agent_msg(session, room_id,
        f"This transaction is currently '{tx.status.value}'. No further action needed.", tx.id))


# ── normal transaction handler ────────────────────────────────────────────────
def _handle_for_tx(session, tx, sender_email, msg_type, content, image_data, out):
    role = "buyer" if sender_email == tx.buyer_email else "seller"
    tid  = tx.id

    # Check for "not satisfied" FIRST — works in any HOLDING/AWAITING_PHOTO state
    if (msg_type == "text" and role == "buyer" and
            tx.status in (TxStatus.HOLDING, TxStatus.AWAITING_PHOTO) and
            return_flow.is_dissatisfied(content)):
        msgs = return_flow.handle_dissatisfied(session, tx, agent_msg)
        out.extend(msgs)
        return

    if tx.status == TxStatus.PROPOSED:
        if role == "seller":
            out.append(agent_msg(session, tx.room_id,
                "Waiting for the buyer to confirm.", tid))
            return
        if CONFIRM_RE.search(content or ""):
            tx.status = TxStatus.COLLECTING_SELLER_DETAILS
            out.append(agent_msg(session, tx.room_id,
                f"Great! @{tx.seller_email.split('@')[0]}, to protect the buyer's funds "
                f"I need to verify your bank account first.\n\n"
                f"Please reply with your:\n"
                f"• 10-digit account number\n"
                f"• 3-digit bank code (e.g. 058 GTBank · 011 First Bank · 033 UBA · 044 Access · 057 Zenith · 999992 Opay · 90267 Kuda)\n\n"
                f"Example: `0123456789 058`", tid))
        elif CANCEL_RE.search(content or ""):
            tx.status = TxStatus.CANCELLED
            out.append(agent_msg(session, tx.room_id, "Cancelled — nothing was charged.", tid))
        else:
            out.append(agent_msg(session, tx.room_id,
                f"To confirm: **{tx.item_description}** for ₦{tx.amount_kobo/100:,.0f}. "
                f"Reply 'yes' to proceed or 'cancel' to back out.", tid))
        return

    if tx.status == TxStatus.COLLECTING_SELLER_DETAILS:
        if role == "buyer":
            out.append(agent_msg(session, tx.room_id,
                "Waiting for the seller to provide their bank details.", tid))
            return
        match = BANK_RE.search(content or "")
        if not match:
            out.append(agent_msg(session, tx.room_id,
                "I need your 10-digit account number and 3-digit bank code together.\n"
                "Example: `0123456789 058`\n\n"
                "Codes: GTBank 058 · First Bank 011 · UBA 033 · Access 044 · Zenith 057 · Opay 999992 · Kuda 90267", tid))
            return
        acct, bank_code = match.group(1), match.group(2)
        try:
            resolved = flw.resolve_account(acct, bank_code)
        except flw.FlutterwaveError as e:
            out.append(agent_msg(session, tx.room_id,
                f"Couldn't verify that account ({e}). Please double-check and try again.", tid))
            return
        tx.seller_account_number = acct
        tx.seller_bank_code      = bank_code
        tx.seller_account_name   = resolved.get("account_name", "Unknown")
        out.append(agent_msg(session, tx.room_id, _create_virtual_account(session, tx), tid))
        return

    if tx.status == TxStatus.AWAITING_PAYMENT:
        out.append(agent_msg(session, tx.room_id,
            f"Still waiting for the bank transfer.\n\n"
            f"🏦 Bank: **{tx.virtual_bank_name}**\n"
            f"📋 Account: **{tx.virtual_account_number}**\n"
            f"Amount: ₦{tx.amount_kobo/100:,.0f}", tid))
        return

    if tx.status == TxStatus.HOLDING:
        if role == "seller" and SHIPPED_RE.search(content or ""):
            tx.status = TxStatus.AWAITING_PHOTO
            out.append(agent_msg(session, tx.room_id,
                f"Noted! @{tx.buyer_email.split('@')[0]}, the seller says the item is on its way. "
                f"Once it arrives, **send a photo of the item** here to release payment.\n\n"
                f"If you're not satisfied with what you receive, type "
                f"**'Not satisfied with the delivery'** and I'll open a return process.", tid))
            return
        result = ai_agent.chat(tx, role, content or effective_text_from_type(msg_type))
        _apply_chat(session, tx, result, out)
        return

    if tx.status == TxStatus.AWAITING_PHOTO:
        if msg_type == "image":
            decision = ai_agent.review_photo(tx, image_data, content or "")
            if decision["decision"] == "approve":
                out.append(agent_msg(session, tx.room_id, _execute_payout(session, tx), tid))
            elif decision["decision"] == "unclear":
                out.append(agent_msg(session, tx.room_id,
                    f"I can't clearly identify the item in that photo. "
                    f"{decision['reason']} — please resend a clear photo of the item.", tid))
            else:
                msgs = return_flow.handle_dissatisfied(session, tx, agent_msg)
                out.extend(msgs)
        elif role == "buyer":
            out.append(agent_msg(session, tx.room_id,
                "Please **send a photo** of the received item to release payment — "
                "tap the camera icon.\n\n"
                "Not happy with what arrived? Type **'Not satisfied with the delivery'**.", tid))
        else:
            out.append(agent_msg(session, tx.room_id,
                "Waiting for the buyer to send a photo confirming delivery.", tid))
        return

    out.append(agent_msg(session, tx.room_id,
        f"Transaction is '{tx.status.value}'. No further action needed.", tid))


def effective_text_from_type(msg_type):
    return "[sent a photo]" if msg_type == "image" else "[sent a sticker]"

def _apply_chat(session, tx, result, out):
    reply, action, reason = result["reply"], result["action"], result["reason"]
    if action == "dispute":
        tx.status = TxStatus.DISPUTED
        reply = reply or f"Flagged for review ({reason}). Funds frozen."
    out.append(agent_msg(session, tx.room_id, reply or "Got it.", tx.id))

def _create_virtual_account(session, tx) -> str:
    ref = f"escrow_{tx.id}_{uuid.uuid4().hex[:8]}"
    try:
        va = flw.create_virtual_account(
            email=tx.buyer_email, amount_naira=tx.amount_kobo / 100, tx_ref=ref)
    except flw.FlutterwaveError as e:
        return (f"Verified seller ({tx.seller_account_name}) but couldn't generate payment "
                f"account right now ({e}). Please try again in a moment.")
    tx.virtual_account_number = va.get("account_number")
    tx.virtual_bank_name      = va.get("bank_name")
    tx.payment_reference      = ref
    tx.status                 = TxStatus.AWAITING_PAYMENT
    return (
        f"✅ Seller verified: **{tx.seller_account_name}**\n\n"
        f"@{tx.buyer_email.split('@')[0]}, transfer exactly **₦{tx.amount_kobo/100:,.0f}** to:\n\n"
        f"🏦 Bank: **{va.get('bank_name')}**\n"
        f"📋 Account: **{va.get('account_number')}**\n\n"
        f"Money goes to Custodia escrow — not the seller — until you confirm delivery."
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
            account_number=tx.seller_account_number, bank_code=tx.seller_bank_code,
            amount_naira=payout_kobo / 100,
            narration=f"Custodia payout — {tx.item_description[:40]}",
            reference=reference,
        )
    except flw.FlutterwaveError as e:
        return f"Payout failed: {e}. Our team will complete it manually."
    tx.status           = TxStatus.RELEASED
    tx.fee_kobo         = fee_kobo
    tx.payout_reference = transfer.get("reference", reference)
    tx.resolved_at      = datetime.utcnow()
    return (
        f"🎉 Photo confirmed — item received!\n\n"
        f"**₦{payout_kobo/100:,.0f}** sent to {tx.seller_account_name}.\n"
        f"Reference: `{tx.payout_reference}`\n"
        f"_(₦{fee_kobo/100:,.0f} escrow fee retained)_"
    )


# ── intent detection ──────────────────────────────────────────────────────────
def _watch_for_deal(session, room_id, out):
    recent = (
        session.query(Message).filter(Message.room_id == room_id)
        .order_by(Message.created_at.desc()).limit(CONTEXT_WINDOW).all()
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
        item_description=item, amount_kobo=amount_kobo, status=TxStatus.PROPOSED,
        detected_from_message_id=out[-1].id if out else None,
    )
    session.add(tx)
    session.flush()
    out.append(agent_msg(session, room_id,
        f"I spotted a deal: **{item}** for **₦{amount_kobo/100:,.0f}** between "
        f"@{buyer_email.split('@')[0]} (buyer) and @{seller_email.split('@')[0]} (seller).\n\n"
        f"@{buyer_email.split('@')[0]}, reply **'yes'** to open escrow. "
        f"You pay the full amount — 1% fee only comes out of the seller's payout on release. "
        f"If you're not satisfied after delivery, a full return process protects you.", tx.id))


# ── REST endpoints ────────────────────────────────────────────────────────────

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
    try:    return jsonify(flw.list_banks()), 200
    except flw.FlutterwaveError as e: return jsonify({"error": str(e)}), 502

@app.get("/users/<email>/prefs")
def get_prefs(email):
    session = get_session()
    pref    = session.get(UserPref, email.lower())
    result  = {"email": email, "accepts_open_dms": bool(pref and pref.accepts_open_dms)}
    session.close()
    return jsonify(result), 200

@app.post("/users/<email>/prefs")
def set_prefs(email):
    data    = request.get_json(force=True)
    accepts = bool(data.get("accepts_open_dms"))
    session = get_session()
    pref    = session.get(UserPref, email.lower())
    if pref:  pref.accepts_open_dms = accepts
    else:     session.add(UserPref(email=email.lower(), accepts_open_dms=accepts))
    session.commit()
    session.close()
    return jsonify({"email": email, "accepts_open_dms": accepts}), 200

@app.post("/dms/start")
def start_dm():
    data = request.get_json(force=True)
    a, b = (data.get("email_a") or "").lower(), (data.get("email_b") or "").lower()
    if not (a and b) or a == b:
        return jsonify({"error": "email_a and email_b required and must differ"}), 400
    room_id = "dm:" + "::".join(sorted([a, b]))
    session = get_session()
    ok, err = dm_permission(session, room_id, a)
    session.close()
    if not ok:
        return jsonify({"error": err}), 403
    return jsonify({"room_id": room_id}), 200

@app.get("/rooms/<room_id>/mode")
def get_room_mode(room_id):
    session = get_session()
    room    = session.get(DmRoom, room_id)
    result  = {
        "room_id":       room_id,
        "mode":          room.mode.value if room else "normal",
        "is_dispute_room": room.is_dispute_room if room else False,
    }
    session.close()
    return jsonify(result), 200

@app.post("/rooms/<room_id>/mode")
def set_room_mode(room_id):
    """Toggle emoji-only mode. Not allowed in dispute rooms."""
    data    = request.get_json(force=True)
    mode_str= data.get("mode", "normal")
    if mode_str not in ("normal", "emoji_only"):
        return jsonify({"error": "mode must be 'normal' or 'emoji_only'"}), 400
    session = get_session()
    room    = get_or_create_dm_room(session, room_id)
    if room.is_dispute_room:
        session.close()
        return jsonify({"error": "Cannot change mode of a dispute room."}), 403
    room.mode = DmRoomMode(mode_str)
    session.commit()
    result = {"room_id": room_id, "mode": room.mode.value}
    session.close()
    return jsonify(result), 200

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

@app.post("/webhooks/flutterwave")
def flutterwave_webhook():
    raw_body = request.get_data()
    if not is_valid_flutterwave_signature(raw_body, request.headers):
        return jsonify({"error": "Invalid signature"}), 401
    event  = request.get_json(force=True)
    tx_ref = event.get("data", {}).get("tx_ref")
    if event.get("event") not in ("charge.completed",) or not tx_ref:
        return jsonify({"ok": True}), 200
    try:
        verified = flw.verify_by_reference(tx_ref)
    except flw.FlutterwaveError:
        return jsonify({"error": "Verification failed"}), 502
    if verified.get("status") == "successful":
        session = get_session()
        tx = session.query(Transaction).filter_by(payment_reference=tx_ref).first()
        if tx and tx.status == TxStatus.AWAITING_PAYMENT:
            tx.status    = TxStatus.HOLDING
            tx.funded_at = datetime.utcnow()
            agent_msg(session, tx.room_id,
                f"💰 Payment received! ₦{tx.amount_kobo/100:,.0f} locked in escrow.\n\n"
                f"@{tx.seller_email.split('@')[0]} — safe to ship the item. "
                f"Confirm here once you've sent it.\n\n"
                f"@{tx.buyer_email.split('@')[0]} — send a **photo** when it arrives to release payment. "
                f"Not happy? Type **'Not satisfied with the delivery'**.", tx.id)
            session.commit()
        session.close()
    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(debug=True, port=5000)
