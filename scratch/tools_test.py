#!/usr/bin/env python3
import time, json, urllib.request

# Send a request WITH tools to measure the tools offset
tools = [
    {
        'type': 'function',
        'function': {
            'name': 'search_web',
            'description': 'Search the web for information',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'Search query'},
                    'limit': {'type': 'integer', 'description': 'Max results'}
                },
                'required': ['query']
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'read_file',
            'description': 'Read a file from the filesystem',
            'parameters': {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'File path'}
                },
                'required': ['path']
            }
        }
    }
]

msg = 'What is the weather in Warsaw? '
body = json.dumps({
    'model': 'Qwen3.8-27B',
    'messages': [{'role': 'user', 'content': msg}],
    'tools': tools,
    'max_tokens': 10
}).encode()

req = urllib.request.Request(
    'http://127.0.0.1:19201/v1/chat/completions',
    data=body,
    headers={'Content-Type': 'application/json'}
)

t0 = time.monotonic()
resp = urllib.request.urlopen(req, timeout=30)
data = json.loads(resp.read())
t1 = time.monotonic()

usage = data.get('usage', {})
tools_chars = len(json.dumps(tools))
print(f'tools_chars={tools_chars} prompt_tokens={usage.get("prompt_tokens")} time={(t1-t0)*1000:.0f}ms')
print(f'msg_chars={len(msg)}')
