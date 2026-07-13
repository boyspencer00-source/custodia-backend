"""
scheduler.py
Runs periodic checks for time-based auto-actions:
  - auto-release when buyer doesn't provide address in 48hr
  - auto-refund when seller doesn't meet their return deadline

Call start_scheduler() once at app startup.
Uses APScheduler (lightweight, no Redis needed).
"""

from datetime import datetime
import logging

log = logging.getLogger(__name__)


def start_scheduler(app):
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        log.warning("APScheduler not installed — time-based auto-actions disabled. "
                    "Add apscheduler to requirements.txt")
        return

    scheduler = BackgroundScheduler()

    def run_checks():
        with app.app_context():
            _check_deadlines()

    scheduler.add_job(run_checks, "interval", minutes=15)
    scheduler.start()
    log.info("Deadline scheduler started.")


def _check_deadlines():
    from db import get_session
    from models import Transaction, TxStatus
    import return_flow

    session = get_session()
    now     = datetime.utcnow()

    def agent_msg(sess, room_id, content, tx_id=None):
        from models import Message, MessageType
        m = Message(room_id=room_id, is_agent=True,
                    message_type=MessageType.TEXT, content=content, transaction_id=tx_id)
        sess.add(m)
        return m

    try:
        # ── Check 1: buyer address deadline expired ───────────────────
        overdue_address = session.query(Transaction).filter(
            Transaction.status == TxStatus.RETURN_REQUESTED,
            Transaction.address_deadline <= now,
            Transaction.buyer_return_address.is_(None),
        ).all()

        for tx in overdue_address:
            log.info(f"Auto-releasing tx {tx.id}: buyer gave no address")
            return_flow.auto_release_for_no_address(session, tx, agent_msg)

        # ── Check 2: seller return deadline expired ───────────────────
        overdue_return = session.query(Transaction).filter(
            Transaction.status == TxStatus.RETURN_IN_TRANSIT,
            Transaction.return_deadline <= now,
        ).all()

        for tx in overdue_return:
            log.info(f"Auto-refunding tx {tx.id}: seller missed return deadline")
            return_flow.auto_refund_for_expired_return(session, tx, agent_msg)

        session.commit()
    except Exception as e:
        log.error(f"Scheduler check failed: {e}")
        session.rollback()
    finally:
        session.close()
