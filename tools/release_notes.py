"""Prints the CHANGELOG section for a tag (vX.Y.Z) as release notes."""
import re
import sys

tag = sys.argv[1].lstrip("v")
text = open("CHANGELOG.md", encoding="utf-8").read()
m = re.search(rf"^## \[?{re.escape(tag)}\]?.*?$(.*?)(?=^## |\Z)", text, re.M | re.S)
print(m.group(1).strip() if m else f"Release {tag}")
