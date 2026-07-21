"""Web Push notification service using pywebpush."""

import json
import logging
import os

from pywebpush import WebPushException, webpush

from core import SessionLocal
from db_models import PushSubscription

logger = logging.getLogger(__name__)

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@localhost")


def _vapid_claims(subscription_endpoint: str) -> dict:
    from urllib.parse import urlparse
    parsed = urlparse(subscription_endpoint)
    return {
        "sub": VAPID_SUBJECT if VAPID_SUBJECT.startswith("mailto:") else f"mailto:{VAPID_SUBJECT}",
        "aud": f"{parsed.scheme}://{parsed.netloc}",
    }


def send_push_notification(title: str, body: str, tag: str = "pymon-alarm") -> None:
    if not VAPID_PRIVATE_KEY:
        logger.warning("VAPID_PRIVATE_KEY not set, skipping push")
        return

    session = SessionLocal()
    try:
        subscriptions = session.query(PushSubscription).all()
    except Exception as e:
        logger.error("Error querying push subscriptions: %s", e)
        session.close()
        return

    if not subscriptions:
        session.close()
        return

    payload = json.dumps({
        "title": title,
        "body": body,
        "icon": "/favicon.svg",
        "badge": "/favicon.svg",
        "tag": tag,
        "requireInteraction": True,
    })

    expired = []
    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {
                        "p256dh": sub.p256dh_key,
                        "auth": sub.auth_key,
                    },
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims=_vapid_claims(sub.endpoint),
            )
        except WebPushException as exc:
            if exc.response and exc.response.status_code in (404, 410):
                logger.info("Removing expired push subscription: %s", sub.endpoint[:60])
                expired.append(sub)
            else:
                logger.error("Push failed: %s", exc)
        except Exception as exc:
            logger.error("Push error: %s", exc)

    if expired:
        try:
            for sub in expired:
                session.delete(sub)
            session.commit()
        except Exception as e:
            logger.error("Error cleaning up expired subs: %s", e)

    session.close()