#!/usr/bin/env python3
"""Fix the inverted formula in calibrate.py."""
with open("/home/pawelw/ctxproxy/scratch/calibrate.py") as f:
    content = f.read()

# The bug: est = rho_eff * chars should be est = chars / rho_eff
# Because rho = chars_per_token, so tokens = chars / rho
content = content.replace(
    "est = rho_eff * chars + msg_overhead * n",
    "est = chars / rho_eff + msg_overhead * n"
)

# Also fix the rho_obs calculation - it's already correct (chars/content_tok)
# But let's verify the update logic makes sense
# rho_obs = chars / content_tok  => this is chars per token, correct
# sess["rho_ew"] = (1-a) * sess["rho_ew"] + a * rho_obs  => EWMA of chars/token, correct

with open("/home/pawelw/ctxproxy/scratch/calibrate.py", "w") as f:
    f.write(content)

print("Fixed: est = chars / rho_eff + msg_overhead * n")
