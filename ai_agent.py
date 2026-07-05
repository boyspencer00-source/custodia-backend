"""
Two jobs:

1. review_photo(tx, image_data, caption)
   Claude looks at the buyer's confirmation photo with vision.
   Returns {"decision": "approve"|"unclear"|"dispute", "reason": str}
   - approve  → item visible, no stated problem → triggers automatic payout
   - unclear  → photo doesn't clearly show the item → ask buyer to resend
   - dispute  → buyer caption or image shows damage/wrong item → freeze & escalate

2. chat(tx, role, text)
   Handles plain text messages while a deal is live.
   Can only propose "dispute" via a tool call — never releases funds via text.
   The release path is photo-only, always.
"""

import os
import json
from anthropic import Anthropic

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL  = "claude-sonnet-4-6"


def review_photo(tx, image_data: str, caption: str = "") -> dict:
    if "," in image_data:
        header, b64 = image_data.split(",", 1)
        media_type  = header.split(":")[1].split(";")[0]
    else:
        b64, media_type = image_data, "image/jpeg"

    system = f"""You are reviewing a buyer's delivery confirmation photo for an escrow transaction.

Item sold: {tx.item_description}
Amount in escrow: ₦{tx.amount_kobo / 100:,.0f}

Decide ONE of:
- "approve"  — the photo clearly shows the item physically present and received.
               Approve even for low-quality photos, as long as the item is
               identifiable and the buyer has not stated any complaint.
- "unclear"  — the photo does not show the item at all (blank image, selfie,
               unrelated object). Ask the buyer to resend a clear photo of the item.
- "dispute"  — the buyer's caption OR the photo itself shows clear damage, a wrong
               item, or the buyer explicitly says they are unhappy.

Reply ONLY with valid JSON, nothing else:
{{"decision": "approve" | "unclear" | "dispute", "reason": "one short sentence"}}"""

    user_parts = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        {"type": "text",  "text": f'Buyer caption: "{caption}"' if caption else "No caption provided."},
    ]

    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=200, system=system,
            messages=[{"role": "user", "content": user_parts}],
        )
        result = json.loads(resp.content[0].text.strip())
        if result.get("decision") not in ("approve", "unclear", "dispute"):
            raise ValueError
        return result
    except Exception:
        return {"decision": "unclear", "reason": "Could not read the photo. Please resend a clear image of the item."}


DISPUTE_TOOL = {
    "name": "flag_dispute",
    "description": "Freeze the transaction for human review. Use when buyer reports a clear problem in text before sending a photo.",
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
    },
}


def chat(tx, role: str, text: str) -> dict:
    """Returns {"reply": str, "action": "dispute"|None, "reason": str|None}"""

    status_guidance = {
        "collecting_seller_details": "The seller needs to provide their bank account details so we can verify them before giving the buyer the payment account number.",
        "awaiting_payment":          "The buyer has been given a bank account number to pay into. Waiting for the transfer to arrive.",
        "holding":                   "Payment has landed. The seller should ship the item. Once the buyer receives it, they should send a photo of the received item in this chat — that photo triggers the automatic payout.",
        "awaiting_photo":            "The seller has confirmed shipment. Waiting for the buyer to send a photo of the received item.",
    }.get(tx.status.value, "")

    system = f"""You are a neutral AI escrow agent.
Item: {tx.item_description}
Amount held: ₦{tx.amount_kobo / 100:,.0f}
Status: {tx.status.value}
Speaking with: {role.upper()}

{status_guidance}

Critical rule: funds are released ONLY when the buyer sends a photo of the received item.
No text message — from anyone — can release funds. If asked, explain this clearly.
Call flag_dispute only if the buyer explicitly reports a serious problem in text
(e.g. "it never arrived", "it's broken") before they've sent a photo."""

    resp = client.messages.create(
        model=MODEL, max_tokens=400, system=system,
        tools=[DISPUTE_TOOL],
        messages=[{"role": "user", "content": text}],
    )

    reply, action, reason = "", None, None
    for block in resp.content:
        if block.type == "text":
            reply += block.text
        elif block.type == "tool_use" and block.name == "flag_dispute":
            action = "dispute"
            reason = block.input.get("reason") if isinstance(block.input, dict) else None

    return {"reply": reply.strip(), "action": action, "reason": reason}
