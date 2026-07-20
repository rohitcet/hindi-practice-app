"""
Hindi Practice App server.
Serves the static app (hindi_practice_3.html + images) and proxies AI
requests to Anthropic so the API key never reaches the browser.
"""

import os
import json
import re
import posixpath
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

import anthropic

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

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


def extract_json(text):
    """Try multiple strategies to extract valid JSON from model output."""
    # Strategy 1: strip markdown fences
    clean = re.sub(r'```json|```', '', text).strip()
    try:
        return json.loads(clean)
    except Exception:
        pass

    # Strategy 2: find first { ... } block
    match = re.search(r'\{.*\}', clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except Exception:
            pass

    # Strategy 3: truncated JSON — patch common tail issues and retry
    patched = clean.rstrip().rstrip(',')
    opens = patched.count('{') - patched.count('}')
    arr_opens = patched.count('[') - patched.count(']')
    in_string = False
    escaped = False
    for ch in patched:
        if escaped:
            escaped = False
            continue
        if ch == '\\':
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        patched += '"'
    patched += ']' * max(arr_opens, 0)
    patched += '}' * max(opens, 0)
    try:
        return json.loads(patched)
    except Exception:
        pass

    return None


class ProxyHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        url_path = urllib.parse.urlparse(self.path).path
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
        if self.path != '/proxy':
            self.send_response(404)
            self.end_headers()
            return

        if client is None:
            self.send_response(500)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(
                {"error": "ANTHROPIC_API_KEY is not configured on the server"}
            ).encode('utf-8'))
            return

        length = int(self.headers.get('Content-Length', 0))
        body   = json.loads(self.rfile.read(length))
        prompt = body.get('prompt', '')

        try:
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}]
            )
            raw_text = msg.content[0].text
            print("Raw response:", raw_text[:200])

            parsed = extract_json(raw_text)
            if parsed is None:
                raise ValueError("Could not parse JSON from model response: " + raw_text[:300])

            result = {"content": [{"text": json.dumps(parsed)}]}
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode('utf-8'))

        except Exception as e:
            print("Error:", e)
            self.send_response(500)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode('utf-8'))

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

    def log_message(self, fmt, *args):
        print(fmt % args)


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 9001))
    server = HTTPServer(('0.0.0.0', port), ProxyHandler)
    print(f"Server running on http://0.0.0.0:{port}")
    server.serve_forever()
