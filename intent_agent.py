import os
from anthropic import Anthropic

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

MODEL = "claude-sonnet-4-6"

EVALUATE_TOOL = {
    "name": "evaluate_conversation",
    "description": "Report whether the people in this conversation have reached a firm, specific agreement to buy and sell something for a stated price.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ready": {
                "type": "boolean",
                "description": "True only if a specific item AND a specific price have both been explicitly agreed by both parties - not merely discussed, asked about, or negotiated without a final yes.",
            },
            "buyer_email": {"type": "string", "description": "Email of whoever is paying. Required if ready is true."},
            "seller_email": {"type": "string", "description": "Email of whoever is providing the item. Required if ready is true."},
            "item_description": {"type": "string", "description": "Short description of what's being sold. Required if ready is true."},
            "amount_naira": {"type": "number", "description": "Agreed price in naira (not kobo). Required if ready is true."},
            "reason": {"type": "string", "description": "One sentence explaining the decision either way."},
        },
        "required": ["ready", "reason"],
    },
}


def build_system_prompt(participants):
    return f"""You are silently monitoring a group chat where people buy and sell things
from each other. Participants seen so far: {', '.join(participants) if participants else 'unknown'}.

Your only job: decide if the two most recent people in a thread have just reached a
FINAL agreement - a specific item, for a specific price, with both sides on board
(e.g. seller states a price and buyer says "deal" / "I'll take it" / "sending now").

Do NOT call ready=true for:
- Browsing, asking prices, or "is this still available"
- Ongoing haggling where no final number has been accepted yet
- Vague interest ("I might be interested")

Only call ready=true once agreement is unambiguous. When in doubt, say false and explain why -
false positives interrupt real conversations, which is worse than being slightly slow to catch a deal.
"""


def evaluate_conversation(recent_messages: list[dict]) -> dict:
    """
    recent_messages: list of {"sender": str, "content": str} in chronological order,
    where sender is an email address or "agent".
    Returns a dict matching the tool schema, always including "ready".
    """
    participants = sorted({m["sender"] for m in recent_messages if m["sender"] != "agent"})

    transcript = "\n".join(f"{m['sender']}: {m['content']}" for m in recent_messages)

    response = client.messages.create(
        model=MODEL,
        max_tokens=400,
        system=build_system_prompt(participants),
        tools=[EVALUATE_TOOL],
        tool_choice={"type": "tool", "name": "evaluate_conversation"},
        messages=[{"role": "user", "content": transcript}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "evaluate_conversation":
            result = dict(block.input)
            result.setdefault("ready", False)
            return result

    return {"ready": False, "reason": "No structured response from model."}
