#!/usr/bin/env python3
path = "/home/pawelw/ctxproxy/scratch/run_tests_v2.py"
with open(path) as f:
    lines = f.readlines()

out = []
for line in lines:
    if line.strip().startswith('MSG_A ='):
        out.append('MSG_A = "The quick brown fox jumps over the lazy dog while the cat watches from the tree branch above and the birds sing their morning song in the distant forest clearing\n')
        out.append('MSG_B = "A large grey elephant walked slowly through the dense tropical jungle carrying a heavy wooden bundle on its back while the river flowed gently beside the ancient stone bridge\n')
        out.append('MSG_C = "The old lighthouse keeper carefully tended his oil lamp every single evening as the ships passed safely along the rocky coastline and the stars began to appear in the darkening sky\n')
        out.append('MSG_D = "The young student sat at her desk studying for the upcoming mathematics examination while the rain tapped softly against the window pane outside\n')
        # Skip old MSG_B and MSG_C lines
        continue
    if line.strip().startswith('MSG_B =') or line.strip().startswith('MSG_C ='):
        continue
    out.append(line)

# Now fix over_limit_msgs to include MSG_D
content = ''.join(out)
old_v1 = '''def over_limit_msgs():
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": MSG_A},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_B},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_C},
    ]'''
new_v1 = '''def over_limit_msgs():
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": MSG_A},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_B},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_C},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_D},
    ]'''
content = content.replace(old_v1, new_v1)

old_v2 = '''def over_limit_msgs_v2():
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": MSG_A},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_B},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_C},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": "And one more message to push us well over the limit for sure"},
    ]'''
new_v2 = '''def over_limit_msgs_v2():
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": MSG_A},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_B},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_C},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": MSG_D},
        {"role": "assistant", "content": "OK"},
        {"role": "user", "content": "Please summarize everything we have discussed so far in detail"},
    ]'''
content = content.replace(old_v2, new_v2)

with open(path, "w") as f:
    f.write(content)
print("Patched messages")

