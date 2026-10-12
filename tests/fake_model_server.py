"""A stand-in for a model server (llama.cpp's llama-server), for the tests that start one for real.

It takes the same arguments, answers /health, and answers a chat request with a fixed search form whose
words are the sentence it was sent. A model file whose name holds "broken" makes it end at once, the way
a real server does when it cannot load its model.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main(argv):
    options = dict(zip(argv[::2], argv[1::2]))
    if "broken" in options.get("--model", ""):
        return 3

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body):
            data = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._send({"status": "ok"})

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
            sentence = request["messages"][-1]["content"].rsplit("Sentence: ", 1)[-1]
            form = {"words": sentence, "types": ["pdf"], "where": "anywhere", "period": "last_month", "modified_after": "",
                    "modified_before": "", "min_size": 1, "min_size_unit": "mb", "max_size": 0, "max_size_unit": "mb",
                    "folder": "", "exact": False, "served_by": options.get("--model", ""),
                    "gpu_layers": options.get("--n-gpu-layers", "")}
            self._send({"choices": [{"message": {"role": "assistant", "content": json.dumps(form)}}]})

    server = ThreadingHTTPServer((options.get("--host", "127.0.0.1"), int(options["--port"])), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
