"""
Hindi Practice App server.
Serves the static app (hindi_practice_3.html + images) and handles the
Stripe subscription checkout/webhook routes (web) and the Google Play
Billing subscription verification route (Android) for the $11/month
paywall. Stripe, Google and Supabase secret keys never reach the browser.
"""

import os
import json
import posixpath
import traceback
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
import stripe

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID")
GOOGLE_PLAY_PACKAGE_NAME = os.environ.get("GOOGLE_PLAY_PACKAGE_NAME", "com.hindipracticepsle.app")
GOOGLE_PLAY_SUBSCRIPTION_ID = os.environ.get("GOOGLE_PLAY_SUBSCRIPTION_ID", "monthly_access")
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

_play_credentials = None


def _get_play_credentials():
    """Lazily build (and cache) a Google service-account credentials object for the Play Developer API."""
    global _play_credentials
    if _play_credentials is None and GOOGLE_SERVICE_ACCOUNT_JSON:
        from google.oauth2 import service_account

        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        _play_credentials = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/androidpublisher"]
        )
    return _play_credentials

def _stripe_get(obj, key, default=None):
    """Newer stripe-python versions dropped dict-style .get() from event objects."""
    try:
        return obj[key]
    except (KeyError, AttributeError, TypeError):
        return default


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml",
}


def get_supabase_user(access_token):
    """Ask Supabase to validate the user's access token. Returns the user dict or None."""
    if not (SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY and access_token):
        return None
    try:
        resp = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "apikey": SUPABASE_PUBLISHABLE_KEY,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException:
        pass
    return None


def _supabase_headers():
    return {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def get_profile(user_id):
    """Fetch a user's profile row using the service/secret key (bypasses RLS). Returns dict or None."""
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/profiles",
        params={"id": f"eq.{user_id}", "select": "*"},
        headers=_supabase_headers(),
        timeout=10,
    )
    rows = resp.json() if resp.status_code == 200 else []
    return rows[0] if rows else None


def find_user_id_by_customer(stripe_customer_id):
    """Look up which Supabase user owns a given Stripe customer id. Returns user id or None."""
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/profiles",
        params={"stripe_customer_id": f"eq.{stripe_customer_id}", "select": "id"},
        headers=_supabase_headers(),
        timeout=10,
    )
    rows = resp.json() if resp.status_code == 200 else []
    return rows[0]["id"] if rows else None


def set_subscription_active(user_id, active, stripe_customer_id=None, stripe_subscription_id=None):
    """Flip is_subscribed (whether this user currently has active paid access) on their profile row."""
    patch = {"is_subscribed": active}
    if stripe_customer_id is not None:
        patch["stripe_customer_id"] = stripe_customer_id
    if stripe_subscription_id is not None:
        patch["stripe_subscription_id"] = stripe_subscription_id
    headers = _supabase_headers()
    headers["Prefer"] = "return=minimal"
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/profiles",
        params={"id": f"eq.{user_id}"},
        headers=headers,
        json=patch,
        timeout=10,
    )


class ProxyHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        url_path = urllib.parse.urlparse(self.path).path
        if url_path == "/pricing":
            self._handle_get_pricing()
            return
        if url_path == "/":
            url_path = "/hindi_practice_3.html"

        rel_path = posixpath.normpath(urllib.parse.unquote(url_path)).lstrip("/")
        file_path = os.path.abspath(os.path.join(BASE_DIR, rel_path))

        if not (file_path == BASE_DIR or file_path.startswith(BASE_DIR + os.sep)) or not os.path.isfile(file_path):
            self.send_response(404)
            self._cors()
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        ext = os.path.splitext(file_path)[1].lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")

        with open(file_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if ext == ".html":
            # Always revalidate the app shell — the packaged Android/iOS app loads this
            # URL directly, so a stale cached copy would silently hide new code/fixes.
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        else:
            self.send_header("Cache-Control", "public, max-age=3600")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/create-checkout-session":
            self._handle_create_checkout_session()
        elif self.path == "/stripe-webhook":
            self._handle_stripe_webhook()
        elif self.path == "/verify-play-purchase":
            self._handle_verify_play_purchase()
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def _handle_get_pricing(self):
        if not (STRIPE_SECRET_KEY and STRIPE_PRICE_ID):
            self._json_response(500, {"error": "Stripe is not configured on the server"})
            return
        try:
            price = stripe.Price.retrieve(STRIPE_PRICE_ID)
            self._json_response(200, {"amount": price.unit_amount, "currency": price.currency})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_create_checkout_session(self):
        if not (STRIPE_SECRET_KEY and STRIPE_PRICE_ID):
            self._json_response(500, {"error": "Stripe is not configured on the server"})
            return

        auth_header = self.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
        user = get_supabase_user(token)
        if not user:
            self._json_response(401, {"error": "Not signed in"})
            return

        origin = f"{'https' if self.headers.get('X-Forwarded-Proto') == 'https' else 'http'}://{self.headers.get('Host')}"

        # Reuse the same Stripe customer across re-subscriptions instead of minting a new one each time.
        profile = get_profile(user["id"])
        existing_customer_id = profile.get("stripe_customer_id") if profile else None

        try:
            checkout_kwargs = {
                "mode": "subscription",
                "line_items": [{"price": STRIPE_PRICE_ID, "quantity": 1}],
                "client_reference_id": user["id"],
                "success_url": f"{origin}/?checkout=success",
                "cancel_url": f"{origin}/?checkout=cancel",
            }
            if existing_customer_id:
                checkout_kwargs["customer"] = existing_customer_id
            else:
                checkout_kwargs["customer_email"] = user.get("email")
            session = stripe.checkout.Session.create(**checkout_kwargs)
            self._json_response(200, {"url": session.url})
        except Exception as e:
            self._json_response(500, {"error": str(e)})

    def _handle_stripe_webhook(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = self.rfile.read(length)
        sig_header = self.headers.get("Stripe-Signature", "")

        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception as e:
            self._json_response(400, {"error": f"Invalid webhook: {e}"})
            return

        event_type = event["type"]
        obj = event["data"]["object"]

        if event_type == "checkout.session.completed":
            user_id = _stripe_get(obj, "client_reference_id")
            customer_id = _stripe_get(obj, "customer")
            subscription_id = _stripe_get(obj, "subscription")
            if user_id:
                set_subscription_active(user_id, True, customer_id, subscription_id)

        elif event_type == "customer.subscription.updated":
            # 'active'/'trialing' -> paying. 'past_due' is a grace period (Stripe is still
            # retrying the card) so access stays on; only a fully lapsed status revokes it.
            customer_id = _stripe_get(obj, "customer")
            status = _stripe_get(obj, "status")
            user_id = find_user_id_by_customer(customer_id)
            if user_id:
                if status in ("active", "trialing", "past_due"):
                    set_subscription_active(user_id, True)
                elif status in ("canceled", "unpaid", "incomplete_expired"):
                    set_subscription_active(user_id, False)

        elif event_type == "customer.subscription.deleted":
            customer_id = _stripe_get(obj, "customer")
            user_id = find_user_id_by_customer(customer_id)
            if user_id:
                set_subscription_active(user_id, False)

        self._json_response(200, {"received": True})

    def _handle_verify_play_purchase(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json_response(400, {"error": "Invalid JSON"})
            return

        purchase_token = body.get("purchaseToken")
        if not purchase_token:
            self._json_response(400, {"error": "Missing purchaseToken"})
            return

        auth_header = self.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
        user = get_supabase_user(token)
        if not user:
            self._json_response(401, {"error": "Not signed in"})
            return

        credentials = _get_play_credentials()
        if not credentials:
            self._json_response(500, {"error": "Google Play is not configured on the server"})
            return

        try:
            import google.auth.transport.requests as google_requests

            credentials.refresh(google_requests.Request())
            headers = {"Authorization": f"Bearer {credentials.token}"}

            sub_resp = requests.get(
                f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
                f"{GOOGLE_PLAY_PACKAGE_NAME}/purchases/subscriptionsv2/tokens/{purchase_token}",
                headers=headers,
                timeout=10,
            )
            sub_resp.raise_for_status()
            sub_data = sub_resp.json()
            state = sub_data.get("subscriptionState")
            is_active = state in ("SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD")

            # Acknowledge on first verification only — Play auto-refunds an unacknowledged
            # subscription after 3 days, and acknowledging twice would just 400 harmlessly,
            # but we only need to try it while it's actually active.
            if is_active:
                ack_state = sub_data.get("acknowledgementState") or (
                    sub_data.get("lineItems", [{}])[0].get("acknowledgementState")
                    if sub_data.get("lineItems") else None
                )
                if ack_state != "ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED":
                    requests.post(
                        f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
                        f"{GOOGLE_PLAY_PACKAGE_NAME}/purchases/subscriptions/{GOOGLE_PLAY_SUBSCRIPTION_ID}/tokens/"
                        f"{purchase_token}:acknowledge",
                        headers=headers,
                        timeout=10,
                    )

            set_subscription_active(user["id"], is_active)
            self._json_response(200, {"success": True, "active": is_active})
        except Exception as e:
            traceback.print_exc()
            self._json_response(500, {"error": str(e)})

    def _json_response(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def log_message(self, fmt, *args):
        print(fmt % args)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 9001))
    server = HTTPServer(("0.0.0.0", port), ProxyHandler)
    print(f"Server running on http://0.0.0.0:{port}")
    server.serve_forever()
