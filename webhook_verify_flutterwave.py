import hmac
import hashlib
import os

FLW_SECRET_HASH = os.getenv("FLW_SECRET_HASH", "")


def is_valid_flutterwave_signature(raw_body: bytes, headers) -> bool:
    """Flutterwave's own docs describe HMAC-SHA256 of the raw body, base64-encoded,
    sent in the 'flutterwave-signature' header. Some dashboards/integrations instead
    send the secret hash directly, unhashed, in a 'verif-hash' header. We check
    whichever is present - never trust a webhook with neither.
    """
    sig_header = headers.get("flutterwave-signature")
    if sig_header:
        import base64
        computed = base64.b64encode(
            hmac.new(FLW_SECRET_HASH.encode("utf-8"), raw_body, hashlib.sha256).digest()
        ).decode("utf-8")
        return hmac.compare_digest(computed, sig_header)

    verif_hash = headers.get("verif-hash")
    if verif_hash:
        return hmac.compare_digest(verif_hash, FLW_SECRET_HASH)

    return False
