#!/usr/bin/env python3
"""Optional controlled endpoint; operator prepares it separately, never a migration hook."""
import argparse
import http.server
import json
import socketserver
import threading
import urllib.parse


def main():
    p=argparse.ArgumentParser(); p.add_argument('--bind',required=True); p.add_argument('--port',type=int,required=True)
    p.add_argument('--identity',required=True); p.add_argument('--echo-port',type=int)
    a=p.parse_args()
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            values=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            payload=json.dumps(dict(endpoint_id=a.identity,nonce=values.get('nonce',[''])[0],peer=self.client_address[0])).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
        def log_message(self,*args): pass
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(10)
            try:
                while True:
                    value=self.request.recv(4096)
                    if not value: break
                    self.request.sendall(value)
            except OSError: pass
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads=True
        allow_reuse_address=True
    if a.echo_port:
        echo=Server((a.bind,a.echo_port),Echo); threading.Thread(target=echo.serve_forever,daemon=True).start()
    http.server.ThreadingHTTPServer((a.bind,a.port),Handler).serve_forever()


if __name__=='__main__': main()
