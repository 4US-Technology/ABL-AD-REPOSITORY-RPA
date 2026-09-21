from __future__ import annotations
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class ServiceState:
    def __init__(self) -> None:
        self.db_ok = False
        self.cycles: dict[str, int] = {"email": 0, "vpn": 0}
        self.failures: dict[str, int] = {"email": 0, "vpn": 0}
        self.active = True
    @property
    def ready(self) -> bool:
        return self.db_ok and all(self.cycles.values())
def start_server(state: ServiceState, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            if self.path == "/livez": payload, code = {"live": state.active}, 200
            elif self.path == "/readyz": payload, code = {"ready": state.ready}, 200 if state.ready else 503
            elif self.path == "/metrics":
                text = "\n".join([f'ad_rpa_flow_cycles_total{{flow="{k}"}} {v}' for k,v in state.cycles.items()] + [f'ad_rpa_flow_failures_total{{flow="{k}"}} {v}' for k,v in state.failures.items()]) + "\n"
                self.send_response(200); self.send_header("Content-Type", "text/plain; version=0.0.4"); self.end_headers(); self.wfile.write(text.encode()); return
            else: payload, code = {"error": "not found"}, 404
            self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(payload).encode())
    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
