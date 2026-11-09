"""Connect to the existing local Chrome debugging socket, without exporting credentials."""
import json
import os
import pathlib
import time
import urllib.request

import websocket


class Chrome:
    def __init__(self, target_id=None):
        marker = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data/DevToolsActivePort"
        if not marker.exists():
            raise RuntimeError("Chrome DevToolsActivePort missing; keep the current debugging-enabled Chrome open.")
        port, route = marker.read_text().splitlines()[:2]
        self.ws = websocket.create_connection("ws://127.0.0.1:" + port + route, timeout=20, suppress_origin=True, http_no_proxy=["127.0.0.1", "localhost"])
        self.ws.settimeout(90)
        self.sequence = 0
        targets = self.call("Target.getTargets")["targetInfos"]
        matches = [t for t in targets if t.get("type") == "page" and t.get("url", "").startswith("https://www.tgegg.com/")]
        if target_id:
            matches = [t for t in targets if t.get("targetId") == target_id]
        if not matches:
            raise RuntimeError("Open https://www.tgegg.com/group-search in the logged-in Chrome first.")
        target = next((t for t in reversed(matches) if "/group-search" in t.get("url", "")), matches[-1])
        self.target_id = target["targetId"]
        self.session = self.call("Target.attachToTarget", {"targetId": self.target_id, "flatten": True})["sessionId"]

    def call(self, method, params=None, session=None):
        self.sequence += 1
        message = {"id": self.sequence, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        while True:
            reply = json.loads(self.ws.recv())
            if reply.get("id") != message["id"]:
                continue
            if "error" in reply:
                raise RuntimeError(str(reply["error"]))
            return reply.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True, "timeout": 85000}, self.session)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise RuntimeError(details.get("exception", {}).get("description", details.get("text", "JavaScript evaluation failed")))
        return result.get("result", {}).get("value")

    def close(self):
        try:
            self.call("Target.detachFromTarget", {"sessionId": self.session})
        finally:
            self.ws.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
