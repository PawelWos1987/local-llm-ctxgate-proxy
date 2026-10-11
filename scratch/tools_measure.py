#!/usr/bin/env python3
import json, urllib.request

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

# Test 1: with tools
body1 = json.dumps({
    'model': 'Qwen3.8-27B',
    'messages': [{'role': 'user', 'content': 'hi'}],
    'tools': tools,
    'max_tokens': 5
}).encode()
req1 = urllib.request.Request('http://127.0.0.1:19201/v1/chat/completions', data=body1, headers={'Content-Type': 'application/json'})
resp1 = urllib.request.urlopen(req1, timeout=30)
data1 = json.loads(resp1.read())
tok_with = data1['usage']['prompt_tokens']

# Test 2: without tools
body2 = json.dumps({
    'model': 'Qwen3.8-27B',
    'messages': [{'role': 'user', 'content': 'hi'}],
    'max_tokens': 5
}).encode()
req2 = urllib.request.Request('http://127.0.0.1:19201/v1/chat/completions', data=body2, headers={'Content-Type': 'application/json'})
resp2 = urllib.request.urlopen(req2, timeout=30)
data2 = json.loads(resp2.read())
tok_without = data2['usage']['prompt_tokens']

tools_chars = len(json.dumps(tools, separators=(',', ':')))
tools_tokens = tok_with - tok_without
print(f'tools_chars={tools_chars} tools_tokens={tools_tokens} chars_per_token={tools_chars/tools_tokens:.2f}')
print(f'tok_with_tools={tok_with} tok_without={tok_without}')
