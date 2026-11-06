"""Mock GitHub/Gitea API server for end-to-end PR testing.

Implements the two endpoints the app uses:
  GET  /user                         -> auth check
  POST /repos/{owner}/{repo}/pulls   -> create PR
"""
import json
import http.server
import threading

CREATED = []


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.endswith("/user"):
            auth = self.headers.get("Authorization", "")
            if auth.startswith("token test-token"):
                self._send(200, {"login": "test-user", "username": "test-user"})
            else:
                self._send(401, {"message": "Bad credentials"})
        elif "/branches/" in self.path:
            # Branch existence endpoint used by the PR pre-flight. Any branch
            # named "missing-branch" 404s so tests can exercise the
            # "branch not found" path; everything else exists.
            name = self.path.rsplit("/", 1)[-1]
            if name == "missing-branch":
                self._send(404, {"message": "Not Found", "documentation_url": ""})
            else:
                self._send(200, {"name": name, "commit": {"sha": "abc123"}})
        else:
            self._send(404, {"message": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else ""
        data = json.loads(raw) if raw else {}
        if "/pulls" in self.path:
            # Simulate a GitHub 422 validation failure for a repo named
            # "failrepo" so we can test error surfacing end-to-end.
            if "/repos/failrepo/" in self.path:
                self._send(422, {
                    "message": "Validation Failed",
                    "errors": [
                        {"resource": "PullRequest", "field": "base", "code": "invalid",
                         "message": "There isn't anything to compare. Your current branch has the same commits as the base branch."},
                    ],
                })
                return
            CREATED.append({"path": self.path, "data": data})
            self._send(201, {
                "number": 101,
                "title": data.get("title"),
                "state": "open",
                "html_url": "https://gitea.example.com/owner/repo/pull/101",
                "url": "https://gitea.example.com/api/v1/repos/owner/repo/pulls/101",
            })
        else:
            self._send(404, {"message": "not found"})


def start(port=9099):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


if __name__ == "__main__":
    import sys
    srv, port = start(int(sys.argv[1]) if len(sys.argv) > 1 else 9099)
    print("mock api on " + str(port), flush=True)
    srv.serve_forever()
