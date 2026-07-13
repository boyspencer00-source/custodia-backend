import enum
import uuid
import secrets
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, Enum, Text, Boolean, ForeignKey
from db import Base


class TxStatus(str, enum.Enum):
    PROPOSED                  = "proposed"
    COLLECTING_SELLER_DETAILS = "collecting_seller_details"
    AWAITING_PAYMENT          = "awaiting_payment"
    HOLDING                   = "holding"
    AWAITING_PHOTO            = "awaiting_photo"
    # ── Return flow statuses ──────────────────────────────────────────
    RETURN_REQUESTED          = "return_requested"   # buyer said not satisfied; waiting for address (48hr)
    RETURN_ADDRESS_PROVIDED   = "return_address_provided"  # buyer gave address; waiting seller to pick window
    RETURN_IN_TRANSIT         = "return_in_transit"  # seller picked window; countdown running
    RETURN_RECEIVED           = "return_received"    # seller sent photo + gave buyer the code
    # ── Terminal statuses ────────────────────────────────────────────
    RELEASED                  = "released"
    REFUNDED                  = "refunded"
    DISPUTED                  = "disputed"
    CANCELLED                 = "cancelled"


CLOSED_STATES = {TxStatus.RELEASED, TxStatus.REFUNDED, TxStatus.DISPUTED, TxStatus.CANCELLED}


class MessageType(str, enum.Enum):
    TEXT    = "text"
    IMAGE   = "image"
    STICKER = "sticker"


class DmRoomMode(str, enum.Enum):
    NORMAL     = "normal"      # standard chat
    EMOJI_ONLY = "emoji_only"  # only emoji characters allowed


def gen_id():
    return str(uuid.uuid4())


def gen_secret_code():
    """6-character alphanumeric secret shared between agent→seller→buyer."""
    return secrets.token_hex(3).upper()   # e.g. "A3F9C2"


class UserPref(Base):
    __tablename__ = "user_prefs"
    email            = Column(String, primary_key=True)
    accepts_open_dms = Column(Boolean, nullable=False, default=False)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DmRoom(Base):
    """Tracks per-room settings (emoji-only mode, dispute flag)."""
    __tablename__ = "dm_rooms"
    room_id        = Column(String, primary_key=True)
    mode           = Column(Enum(DmRoomMode), nullable=False, default=DmRoomMode.NORMAL)
    is_dispute_room= Column(Boolean, nullable=False, default=False)  # dispute rooms ignore emoji-only
    created_at     = Column(DateTime, default=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    room_id        = Column(String, nullable=False, index=True, default="global")
    sender_email   = Column(String, nullable=True)
    is_agent       = Column(Boolean, default=False)
    message_type   = Column(Enum(MessageType), nullable=False, default=MessageType.TEXT)
    content        = Column(Text, nullable=True)
    image_data     = Column(Text, nullable=True)
    sticker_id     = Column(String, nullable=True)
    transaction_id = Column(String, ForeignKey("escrow_transactions.id"), nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = "escrow_transactions"
    id                     = Column(String, primary_key=True, default=gen_id)
    room_id                = Column(String, nullable=False, index=True, default="global")

    buyer_email            = Column(String, nullable=False)
    seller_email           = Column(String, nullable=False)
    item_description       = Column(Text, nullable=False)

    amount_kobo            = Column(Integer, nullable=False)
    fee_kobo               = Column(Integer, nullable=True)

    # Seller payout details
    seller_bank_code       = Column(String, nullable=True)
    seller_account_number  = Column(String, nullable=True)
    seller_account_name    = Column(String, nullable=True)

    # Buyer refund details (collected during return flow)
    buyer_bank_code        = Column(String, nullable=True)
    buyer_account_number   = Column(String, nullable=True)
    buyer_account_name     = Column(String, nullable=True)

    # Virtual account for buyer payment
    virtual_account_number = Column(String, nullable=True)
    virtual_bank_name      = Column(String, nullable=True)
    payment_reference      = Column(String, nullable=True, unique=True)
    payout_reference       = Column(String, nullable=True)

    # Return flow fields
    dispute_room_id        = Column(String, nullable=True)   # the 3-way DM created on dispute
    buyer_return_address   = Column(Text, nullable=True)     # address buyer provided
    return_window_hours    = Column(Integer, nullable=True)  # seller-chosen window in hours
    return_deadline        = Column(DateTime, nullable=True) # when the countdown expires
    return_secret_code     = Column(String, nullable=True)   # shown only to seller
    address_deadline       = Column(DateTime, nullable=True) # 48hr for buyer to give address

    status                 = Column(Enum(TxStatus), nullable=False, default=TxStatus.PROPOSED)
    detected_from_message_id = Column(Integer, nullable=True)
    created_at             = Column(DateTime, default=datetime.utcnow)
    funded_at              = Column(DateTime, nullable=True)
    resolved_at            = Column(DateTime, nullable=True)
