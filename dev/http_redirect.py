"""Отвечает 301 на https:// с тем же хостом и путём. Запуск: http_redirect.py <адрес> <порт>."""
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Redirect(BaseHTTPRequestHandler):
    def do_GET(self):
        host = (self.headers.get("Host") or self.server.server_address[0]).split(":")[0]
        self.send_response(301)
        self.send_header("Location", f"https://{host}{self.path}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer((sys.argv[1], int(sys.argv[2])), Redirect).serve_forever()
