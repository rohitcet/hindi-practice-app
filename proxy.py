"""
Hindi Practice App server.
Serves the static app (hindi_practice_3.html + images) and handles the
Stripe checkout / webhook routes for the lifetime-access paywall. Stripe
and Supabase secret keys never reach the browser.
"""

import os
import json
import posixpath
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

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

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


def grant_lifetime_access(user_id, stripe_customer_id):
    """Flip is_lifetime on a user's profile row using the service/secret key (bypasses RLS)."""
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/profiles",
        params={"id": f"eq.{user_id}"},
        headers={
            "apikey": SUPABASE_SECRET_KEY,
            "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        json={"is_lifetime": True, "stripe_customer_id": stripe_customer_id},
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
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path == "/create-checkout-session":
            self._handle_create_checkout_session()
        elif self.path == "/stripe-webhook":
            self._handle_stripe_webhook()
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

        try:
            session = stripe.checkout.Session.create(
                mode="payment",
                line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
                client_reference_id=user["id"],
                customer_email=user.get("email"),
                success_url=f"{origin}/?checkout=success",
                cancel_url=f"{origin}/?checkout=cancel",
            )
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

        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            user_id = session.get("client_reference_id")
            customer_id = session.get("customer")
            if user_id:
                grant_lifetime_access(user_id, customer_id)

        self._json_response(200, {"received": True})

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
