#!/usr/bin/env python3
"""
Publishes Bee's Dashboard as a static website on GitHub Pages, from the engine server.

Called by binance_sync.py after every sync that produced a new data package, or by hand:
    python3 ~/BinanceData/bin/publish_site.py

Config  ~/BinanceData/site.json        {"repo": "owner/name", "domain": "dashboard.example.com", "branch": "main"}
Token   ~/BinanceData/.github_token    fine-grained GitHub token (Contents: read & write on that repo only), chmod 600

A full publish (after each daily rebuild) replaces the repository's content with a single fresh commit, so the
repository stays the size of one data package. Between full publishes, the 15-minute refresh adds small commits
with only the files that changed (prices, 4h charts, alerts data); the next full publish squashes them away.
"""
import base64, hashlib, json, os, re, shutil, stat, subprocess, sys, urllib.request
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CFG = os.path.join(ROOT, "site.json")
TOKEN = os.path.join(ROOT, ".github_token")
PAGE = os.path.join(ROOT, "web", "index.html")
EXPORT = os.path.join(ROOT, "web_export")
BUILD = os.path.join(ROOT, "site_build")

ICON = ("data:image/svg+xml," + "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' "
        "rx='7' fill='%231f5f8b'/%3E%3Cpath d='M7 22l6-7 5 4 7-9' stroke='white' stroke-width='3' fill='none' "
        "stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E")
RESET = (":root{color-scheme:light;padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}"
         "body{margin:0;font:14px system-ui,-apple-system,sans-serif}img{max-width:100%}[hidden]{display:none!important}")


def ask_origin(cfg):
    """The public Ask relay (optional): must be a plain https URL; returns (url, origin) or (None, None)."""
    url = (cfg or {}).get("ask_endpoint") or ""
    m = re.fullmatch(r"(https://[a-z0-9.-]+\.[a-z]{2,})(/[A-Za-z0-9._/-]*)?", url)
    return (url, m.group(1)) if m else (None, None)


def csp_for(page, extra_connect=None):
    """Strict Content-Security-Policy: only this page's own inline script (pinned by hash), the pinned
    chart library from unpkg, Google Fonts, and same-origin data files. No other script, frame,
    form target or connection can load."""
    hashes = []
    for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page, re.S):
        hashes.append("'sha256-" + base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode() + "'")
    return ("default-src 'none'; script-src https://unpkg.com " + " ".join(hashes) + "; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'" + (" " + extra_connect if extra_connect else "") +
            "; base-uri 'none'; form-action 'none'; manifest-src 'none'")


def wrap(page, ask_url=None, ask_org=None):
    """The page source is written for the Claude viewer (no <html>/<head>). Turn it into a full document:
    everything before the first <header> (title, meta, fonts, style) goes into <head>."""
    cut = page.find("<header")
    head, body = (page[:cut], page[cut:]) if cut > 0 else ("", page)
    return ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            f"<meta http-equiv=\"Content-Security-Policy\" content=\"{csp_for(page, ask_org)}\">\n"
            + (f"<meta name=\"ask-endpoint\" content=\"{ask_url}\">\n" if ask_url else "") +
            "<meta name=\"referrer\" content=\"no-referrer\">\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">\n"
            "<meta name=\"robots\" content=\"noindex,nofollow\">\n"
            f"<link rel=\"icon\" href=\"{ICON}\">\n<style>{RESET}</style>\n{head}\n</head>\n<body>\n{body}\n</body>\n</html>\n")


def read_token():
    """Token file must be private to this user; tighten it if it is not, refuse symlinks."""
    if not os.path.exists(TOKEN):
        return None
    if os.path.islink(TOKEN):
        raise RuntimeError("publish_site: .github_token must be a regular file, not a link")
    mode = stat.S_IMODE(os.stat(TOKEN).st_mode)
    if mode & 0o077:
        os.chmod(TOKEN, 0o600)
    with open(TOKEN) as f:
        tok = f.read().strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{20,255}", tok or ""):
        raise RuntimeError("publish_site: .github_token does not look like a single GitHub token")
    return tok


def token_expiry(token, repo):
    """Expiry date of a fine-grained token (GitHub reports it in a response header). Best effort."""
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}",
                                     headers={"Authorization": f"Bearer {token}", "User-Agent": "BeesDashboard"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.headers.get("github-authentication-token-expiration")
    except Exception:
        return None


def git_version(env):
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True, env=env).stdout
        m = re.search(r"(\d+)\.(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except Exception:
        return (0, 0)


def git(args, cwd, env=None, check=True):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        # never echo the auth header
        raise RuntimeError(f"git {args[0]} failed: {p.stderr.strip()[-400:]}")
    return p


def _push(remote, branch, token, env, force):
    args = ["push", "-q"] + (["-f"] if force else []) + [remote, f"{branch}:{branch}"]
    push_env = dict(env)
    if token:
        # The credential goes in through the environment (readable only by this user), never on the command
        # line (visible in the process list) and never in a URL or a file.
        b64 = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        header = f"AUTHORIZATION: basic {b64}"
        if git_version(env) >= (2, 31):
            push_env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                            GIT_CONFIG_VALUE_0=header)
            git(args, BUILD, push_env)
        else:                                  # older git: header on the command line (still never in a URL or file)
            git(["-c", f"http.https://github.com/.extraheader={header}"] + args, BUILD, push_env)
    else:
        git(args, BUILD, push_env)


def _sync_tree(dst_data):
    """Copy only changed files from the export into the build tree; drop files that left the export.
    Returns the number of files changed."""
    import filecmp
    changed = 0
    keep = set()
    for d, _, files in os.walk(EXPORT):
        for f in files:
            if f.endswith(".tmp"):
                continue
            src = os.path.join(d, f)
            rel = os.path.relpath(src, EXPORT)
            keep.add(rel)
            dst = os.path.join(dst_data, rel)
            if not os.path.exists(dst) or not filecmp.cmp(src, dst, shallow=False):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                changed += 1
    for d, _, files in os.walk(dst_data):
        for f in files:
            rel = os.path.relpath(os.path.join(d, f), dst_data)
            if rel not in keep:
                os.remove(os.path.join(d, f))
                changed += 1
    return changed


def _write_static(cfg, domain):
    with open(PAGE, encoding="utf-8") as f:
        ask_url, ask_org = ask_origin(cfg)
        html = wrap(f.read(), ask_url, ask_org)
    for name, text in (("index.html", html), (".nojekyll", ""), ("robots.txt", "User-agent: *\nDisallow: /\n")) + \
            ((("CNAME", domain.strip() + "\n"),) if domain else ()):
        path = os.path.join(BUILD, name)
        if not os.path.exists(path) or open(path, encoding="utf-8").read() != text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)


def run(log=print, light=False):
    """Full publish (light=False): a single fresh commit, force-pushed, so the site repository never grows.
    Light publish (light=True, used by the 15-minute refresh): commit only the files that changed on top of the
    last publish and push normally, so a price update uploads kilobytes instead of the whole data package.
    A light publish falls back to a full one when there is no previous publish to build on."""
    if os.path.exists(os.path.join(ROOT, ".no_publish")):
        log("publish_site: .no_publish present, skipping (this machine is not the publisher)")
        return False
    if not os.path.exists(CFG):
        log("publish_site: no site.json, skipping (website not configured)")
        return False
    cfg = json.load(open(CFG))
    repo, branch, domain = cfg["repo"], cfg.get("branch", "main"), cfg.get("domain")
    remote = cfg.get("remote_url") or f"https://github.com/{repo}.git"
    if not os.path.exists(os.path.join(EXPORT, "overview.json")) or not os.path.exists(PAGE):
        log("publish_site: data package or page missing, skipping")
        return False
    token = read_token()
    if not token and remote.startswith("https://"):
        raise RuntimeError("publish_site: ~/BinanceData/.github_token is missing")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    ov = json.load(open(os.path.join(EXPORT, "overview.json")))
    data_msg = f"Data through {ov.get('synced_through') or ov.get('last_date')} (built {ov.get('generated_utc')})"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if light and os.path.isdir(os.path.join(BUILD, ".git")):
        n = _sync_tree(os.path.join(BUILD, "data"))
        _write_static(cfg, domain)
        git(["add", "-A"], BUILD, env)
        if not git(["status", "--porcelain"], BUILD, env).stdout.strip():
            return False                       # nothing changed since the last publish
        git(["-c", "user.name=Bee's Dashboard", "-c", "user.email=dashboard@localhost", "commit", "-q", "-m",
             f"Refresh {stamp}: {n} file(s) - {data_msg}"], BUILD, env)
        try:
            _push(remote, branch, token, env, force=False)
            log(f"publish_site: refreshed {n} file(s) on {repo}")
            _status(repo, domain, ov, stamp, None)
            return True
        except RuntimeError as e:              # the site changed elsewhere or the history is gone: rebuild fully
            log(f"publish_site: light refresh rejected ({str(e)[:120]}), doing a full publish")

    expires = token_expiry(token, repo) if token else None
    shutil.rmtree(BUILD, ignore_errors=True)
    shutil.copytree(EXPORT, os.path.join(BUILD, "data"), ignore=shutil.ignore_patterns("*.tmp"))
    _write_static(cfg, domain)
    git(["init", "-q"], BUILD, env)
    git(["checkout", "-q", "-b", branch], BUILD, env)
    git(["add", "-A"], BUILD, env)
    git(["-c", "user.name=Bee's Dashboard", "-c", "user.email=dashboard@localhost", "commit", "-q", "-m", data_msg], BUILD, env)
    _push(remote, branch, token, env, force=True)
    _status(repo, domain, ov, stamp, expires)
    log(f"publish_site: pushed to {repo} ({data_msg})" + (f" -> https://{domain}" if domain else ""))
    return True


def _status(repo, domain, ov, stamp, expires):
    path = os.path.join(ROOT, "site_status.json")
    old = {}
    try:
        with open(path) as f:
            old = json.load(f)
    except Exception:
        pass
    with open(path, "w") as f:
        json.dump({"published_utc": stamp, "repo": repo, "domain": domain, "data_through": ov.get("synced_through"),
                   "token_expires_utc": expires or old.get("token_expires_utc")}, f)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)
