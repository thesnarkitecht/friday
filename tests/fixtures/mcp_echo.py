# A minimal MCP server over stdio, for tests: one tool, `echo`.
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method, result = msg["method"], {}
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "echo", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo text back", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}
    elif method == "tools/call":
        # Ask the client something first, as real servers may; the client must answer and carry on.
        print(json.dumps({"jsonrpc": "2.0", "id": "srv-1", "method": "roots/list"}), flush=True)
        sys.stdin.readline()
        text = msg["params"]["arguments"]["text"]
        result = {"content": [{"type": "text", "text": "echo: " + text}], "isError": text == "fail"}
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
