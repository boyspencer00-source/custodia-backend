import enum
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Integer, DateTime, Enum, Text, Boolean, ForeignKey
from db import Base


class TxStatus(str, enum.Enum):
    PROPOSED              = "proposed"               # AI spotted a deal, awaiting buyer confirmation
    COLLECTING_SELLER_DETAILS = "collecting_seller_details"  # buyer confirmed; agent asking seller for bank info
    AWAITING_PAYMENT      = "awaiting_payment"       # seller details verified; virtual account given to buyer
    HOLDING               = "holding"                # payment landed in your FLW balance; seller told to ship
    AWAITING_PHOTO        = "awaiting_photo"         # seller confirmed shipment; waiting for buyer's photo
    RELEASED              = "released"               # payout sent to seller (amount − 1%)
    DISPUTED              = "disputed"               # frozen for human review
    CANCELLED             = "cancelled"              # cancelled before payment


CLOSED_STATES = {TxStatus.RELEASED, TxStatus.DISPUTED, TxStatus.CANCELLED}


class MessageType(str, enum.Enum):
    TEXT    = "text"
    IMAGE   = "image"
    STICKER = "sticker"


def gen_id():
    return str(uuid.uuid4())


class UserPref(Base):
    __tablename__ = "user_prefs"
    email            = Column(String, primary_key=True)
    accepts_open_dms = Column(Boolean, nullable=False, default=False)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Message(Base):
    __tablename__ = "messages"
    id             = Column(Integer, primary_key=True, autoincrement=True)
    room_id        = Column(String, nullable=False, index=True, default="global")
    sender_email   = Column(String, nullable=True)
    is_agent       = Column(Boolean, default=False)
    message_type   = Column(Enum(MessageType), nullable=False, default=MessageType.TEXT)
    content        = Column(Text, nullable=True)
    image_data     = Column(Text, nullable=True)   # base64 data URL
    sticker_id     = Column(String, nullable=True)
    transaction_id = Column(String, ForeignKey("escrow_transactions.id"), nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)


class Transaction(Base):
    __tablename__ = "escrow_transactions"
    id                    = Column(String, primary_key=True, default=gen_id)
    room_id               = Column(String, nullable=False, index=True, default="global")

    buyer_email           = Column(String, nullable=False)
    seller_email          = Column(String, nullable=False)
    item_description      = Column(Text, nullable=False)

    # All amounts in kobo (100 kobo = 1 naira)
    amount_kobo           = Column(Integer, nullable=False)
    fee_kobo              = Column(Integer, nullable=True)   # 1% — set only at release

    # Seller payout details — collected AFTER buyer confirms, before payment link
    seller_bank_code      = Column(String, nullable=True)
    seller_account_number = Column(String, nullable=True)
    seller_account_name   = Column(String, nullable=True)   # resolved from Flutterwave

    # Virtual account given to buyer for payment
    virtual_account_number = Column(String, nullable=True)
    virtual_bank_name      = Column(String, nullable=True)
    payment_reference      = Column(String, nullable=True, unique=True)  # tx_ref we sent to FLW

    # Transfer reference once payout is sent
    payout_reference      = Column(String, nullable=True)

    status                = Column(Enum(TxStatus), nullable=False, default=TxStatus.PROPOSED)
    detected_from_message_id = Column(Integer, nullable=True)
    created_at            = Column(DateTime, default=datetime.utcnow)
    funded_at             = Column(DateTime, nullable=True)
    resolved_at           = Column(DateTime, nullable=True)
