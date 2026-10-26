"""Fails if private settings, keys, databases or data exports were committed. These live only on the server."""
import os
import re
import subprocess
import sys

FORBIDDEN = [r"(^|/)mm\.json$", r"(^|/)live\.json$", r"(^|/)\.github_token$", r"(^|/)\.telegram$", r"(^|/)\.binance_",
             r"(^|/)\.mexc_", r"\.db(-wal|-shm)?$", r"(^|/)web_export/", r"(^|/)site_build/", r"(^|/)data/",
             r"(^|/)logs/", r"(^|/)site_status\.json$", r"(^|/)status\.json$", r"(^|/)public/shortlist\.json$",
             r"\.(pem|key|p12)$", r"(^|/)id_(rsa|ed25519)"]
files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split("\n")
bad = [f for f in files if f and any(re.search(p, f) for p in FORBIDDEN)]
big = [f for f in files if f and os.path.exists(f) and os.path.getsize(f) > 5_000_000]
if bad or big:
    print("Private or oversized files in the repository:")
    for f in bad:
        print("  private:", f)
    for f in big:
        print("  over 5 MB:", f)
    sys.exit(1)
print(f"ok: {len([f for f in files if f])} tracked files, none private")
