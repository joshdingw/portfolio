"""Run with: python app.py. Python standard library only."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlparse

from portfolio.demo import demo_state
from portfolio.market import SOURCE, fetch_security
from portfolio.storage import Store

ROOT = Path(__file__).resolve().parent


def make_server(store, port):
    token = secrets.token_urlsafe(32)
    job = {"running": False, "done": 0, "total": 0, "errors": [], "success": []}
    lock = threading.Lock()

    def refresh(codes, benchmark):
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(fetch_security, code, code == benchmark): code for code in codes}
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        quote, rows = future.result()
                        store.save_market(code, quote, rows, SOURCE)
                        with lock:
                            job["success"].append(code)
                    except Exception as error:
                        with lock:
                            job["errors"].append(dict(symbol=code, message=str(error)[:220]))
                    finally:
                        with lock:
                            job["done"] += 1
        finally:
            with lock:
                job["running"] = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send(self, value, status=200, content_type="application/json; charset=utf-8", filename=None):
            body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() if not isinstance(value, bytes) else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def do_GET(self):
            if not self.valid_host():
                return self.send({"error": "仅允许本机访问"}, 403)
            path = urlparse(self.path).path
            if path == "/api/state":
                state = store.state()
                state["token"] = token
                return self.send(state)
            if path == "/api/demo":
                return self.send(demo_state())
            if path == "/api/refresh-status":
                with lock:
                    return self.send(job.copy())
            if path == "/api/backup":
                return self.send(json.dumps(store.backup(), ensure_ascii=False, indent=2).encode(), filename="portfolio-backup.json")
            if path == "/api/template":
                sample = "symbol,name,quantity,cost,industry,effective_date\nsh600519,贵州茅台,100,1500,消费,2026-01-01\n"
                return self.send(sample.encode("utf-8-sig"), content_type="text/csv; charset=utf-8", filename="holdings-template.csv")
            files = {"/": ("index.html", "text/html; charset=utf-8"),
                     "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                     "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in files:
                name, mime = files[path]
                return self.send((ROOT / "static" / name).read_bytes(), content_type=mime)
            self.send({"error": "页面不存在"}, 404)

        def do_POST(self):
            origin = self.headers.get("Origin")
            if not self.valid_host() or self.headers.get("X-Portfolio-Token") != token or (origin and origin not in {
                f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}):
                return self.send({"error": "请求校验失败，请刷新页面"}, 403)
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 1_000_000:
                    raise ValueError("请求为空或超过1MB")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求格式应为对象")
                path = urlparse(self.path).path
                if path == "/api/holdings":
                    store.save_holdings(data.get("holdings"))
                elif path == "/api/delete":
                    store.delete(data.get("symbol"))
                elif path == "/api/settings":
                    store.settings(data)
                elif path == "/api/manual-quote":
                    store.manual_quote(data)
                elif path == "/api/import":
                    reader = csv.DictReader(io.StringIO(str(data.get("csv", "")).lstrip("\ufeff")))
                    if not {"symbol", "quantity", "cost"}.issubset(reader.fieldnames or []):
                        raise ValueError("CSV必须包含 symbol、quantity、cost 列，请下载模板")
                    rows = list(reader)
                    for row in rows:
                        if not row.get("effective_date"):
                            row.pop("effective_date", None)
                    store.save_holdings(rows)
                elif path == "/api/snapshot":
                    return self.send(dict(ok=True, date=store.snapshot()))
                elif path == "/api/refresh":
                    state = store.state()
                    benchmark = state["settings"]["benchmark"]
                    codes = [h["symbol"] for h in state["summary"]["holdings"]] + [benchmark]
                    with lock:
                        if job["running"]:
                            return self.send({"error": "行情更新正在进行"}, 409)
                        job.update(running=True, done=0, total=len(codes), errors=[], success=[])
                    threading.Thread(target=refresh, args=(codes, benchmark), daemon=True).start()
                    return self.send(dict(ok=True), 202)
                else:
                    return self.send({"error": "接口不存在"}, 404)
                self.send(dict(ok=True))
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                self.send({"error": str(error)}, 400)
            except Exception:
                self.send({"error": "保存失败，请检查本地数据库是否可写"}, 500)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description="A-share portfolio local monitor")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=ROOT / "data" / "portfolio.db")
    args = parser.parse_args()
    server = make_server(Store(args.data), args.port)
    print(f"Portfolio is ready: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
