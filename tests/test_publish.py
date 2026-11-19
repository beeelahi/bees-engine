"""Full publish = one fresh commit; light refresh = only changed files on top; deletions propagate."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests import _paths  # noqa: F401
import publish_site as P


@unittest.skipUnless(shutil.which("git"), "git not installed")
class PublishTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.remote = os.path.join(self.d, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)
        r = os.path.join(self.d, "root")
        os.makedirs(r + "/web_export/pairs"); os.makedirs(r + "/web")
        self.saved = {k: getattr(P, k) for k in ("ROOT", "CFG", "TOKEN", "PAGE", "EXPORT", "BUILD")}
        for k, v in dict(ROOT=r, CFG=r + "/site.json", TOKEN=r + "/.github_token", PAGE=r + "/web/index.html",
                         EXPORT=r + "/web_export", BUILD=r + "/site_build").items():
            setattr(P, k, v)
        json.dump({"repo": "x/y", "remote_url": self.remote, "branch": "main"}, open(r + "/site.json", "w"))
        open(r + "/web/index.html", "w").write("<title>t</title><header>h</header><script>1</script>")
        json.dump({"last_date": "2026-10-25", "generated_utc": "z"}, open(r + "/web_export/overview.json", "w"))
        open(r + "/web_export/pairs/s00.json", "w").write("A" * 1000)
        self.r = r

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(P, k, v)
        shutil.rmtree(self.d, ignore_errors=True)

    def log(self):
        return subprocess.run(["git", "--git-dir", self.remote, "log", "--oneline", "main"], capture_output=True, text=True).stdout.splitlines()

    def files(self):
        return subprocess.run(["git", "--git-dir", self.remote, "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True).stdout.split()

    def test_full_then_light(self):
        q = lambda *a: None
        P.LIGHT_MIN_S, saved = 0, P.LIGHT_MIN_S
        self.addCleanup(setattr, P, "LIGHT_MIN_S", saved)
        self.assertTrue(P.run(q))
        self.assertFalse(P.run(q, light=True))                    # nothing changed
        open(self.r + "/web_export/prices.json", "w").write("{}")
        self.assertTrue(P.run(q, light=True))
        os.remove(self.r + "/web_export/pairs/s00.json")
        self.assertTrue(P.run(q, light=True))
        self.assertEqual(len(self.log()), 3)
        self.assertIn("data/prices.json", self.files())
        self.assertNotIn("data/pairs/s00.json", self.files())
        self.assertTrue(P.run(q))                                  # full publish squashes history
        self.assertEqual(len(self.log()), 1)

    def test_local_only_builds_and_pushes_nothing(self):
        open(self.r + "/.publish_local_only", "w").close()          # site served from the server: no token needed
        q = lambda *a: None
        self.assertTrue(P.run(q))
        self.assertEqual(self.log(), [])                           # nothing reached the remote
        self.assertTrue(os.path.exists(self.r + "/site_build/index.html"))
        self.assertTrue(os.path.exists(self.r + "/site_build/data/overview.json"))
        open(self.r + "/web_export/prices.json", "w").write("{}")
        self.assertTrue(P.run(q, light=True))                      # light refresh: the local copy updates
        self.assertTrue(os.path.exists(self.r + "/site_build/data/prices.json"))
        self.assertEqual(self.log(), [])
        os.remove(self.r + "/.publish_local_only")                 # switched back: GitHub again
        self.assertTrue(P.run(q))
        self.assertEqual(len(self.log()), 1)

    def test_light_held_within_30_minutes(self):
        q = lambda *a: None
        self.assertTrue(P.run(q))                                  # full publish: always goes
        open(self.r + "/web_export/prices.json", "w").write("{}")
        self.assertFalse(P.run(q, light=True))                     # minutes later: held
        self.assertEqual(len(self.log()), 1)
        st = json.load(open(self.r + "/site_status.json"))
        st["published_utc"] = "2020-01-01T00:00:00Z"               # last push long ago: goes, with the held change
        json.dump(st, open(self.r + "/site_status.json", "w"))
        self.assertTrue(P.run(q, light=True))
        self.assertIn("data/prices.json", self.files())

    def test_no_publish_switch(self):
        open(self.r + "/.no_publish", "w").close()
        self.assertFalse(P.run(lambda *a: None))


class PublishHealthTest(unittest.TestCase):
    """ops.sh health: fresh local files are not enough, the website must actually have been pushed."""
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.saved = {k: getattr(P, k) for k in ("ROOT", "CFG")}
        P.ROOT, P.CFG = self.d, self.d + "/site.json"
        json.dump({"repo": "x/y"}, open(P.CFG, "w"))
        self.now = 1_800_000_000                                     # 2027-01-15 08:00 UTC

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(P, k, v)
        shutil.rmtree(self.d, ignore_errors=True)

    def status(self, mins_ago, expires="2027-02-01 00:00:00 UTC"):
        from datetime import datetime, timezone
        t = datetime.fromtimestamp(self.now - mins_ago * 60, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        json.dump({"published_utc": t, "token_expires_utc": expires}, open(self.d + "/site_status.json", "w"))

    def test_recent_push_is_healthy(self):
        self.status(25)
        ok, msg = P.publish_health(self.now)
        self.assertTrue(ok, msg)
        self.assertIn("25 min ago", msg)
        self.assertIn("token expires 2027-02-01", msg)

    def test_pushes_failing_for_hours_is_unhealthy(self):
        self.status(4 * 60)                                         # the 16-19 Nov case: data fresh, no push
        ok, msg = P.publish_health(self.now)
        self.assertFalse(ok)
        self.assertIn("not published for 4.0 h", msg)

    def test_no_record_is_unhealthy(self):
        self.assertFalse(P.publish_health(self.now)[0])

    def test_token_about_to_expire(self):
        self.status(10, expires="2027-01-16 08:00:00 UTC")         # one day left
        ok, msg = P.publish_health(self.now)
        self.assertFalse(ok)
        self.assertIn("token expires", msg)
        self.status(10, expires=None)                               # unknown expiry: no opinion
        self.assertTrue(P.publish_health(self.now)[0])

    def test_not_a_github_publisher(self):
        for flag in (".no_publish", ".publish_local_only"):
            open(self.d + "/" + flag, "w").close()
            self.assertTrue(P.publish_health(self.now)[0])
            os.remove(self.d + "/" + flag)
        os.remove(P.CFG)
        self.assertTrue(P.publish_health(self.now)[0])

    def test_cli_exit_code(self):
        r = subprocess.run([__import__("sys").executable, os.path.join(os.path.dirname(P.__file__), "publish_site.py"),
                            "--health"], capture_output=True, text=True, timeout=30)
        self.assertIn(r.returncode, (0, 1))                         # runs against the repo checkout; no network
        self.assertTrue(r.stdout.strip())


if __name__ == "__main__":
    unittest.main()


class AuthConfigTest(unittest.TestCase):
    """Accounts: only the public Supabase key may ever reach the page; the CSP opens exactly that origin."""

    def cfg(self, obj):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        p = os.path.join(d, "auth.json")
        with open(p, "w") as f:
            json.dump(obj, f)
        return p

    @staticmethod
    def jwt(role):
        import base64
        enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
        return enc({"alg": "HS256"}) + "." + enc({"role": role, "iss": "supabase"}) + ".c2lnbmF0dXJlc2lnbmF0dXJl"

    def test_empty_or_missing_is_off(self):
        self.assertEqual(P.auth_config("/nonexistent/auth.json"), (None, None))
        self.assertEqual(P.auth_config(self.cfg({"url": "", "key": ""})), (None, None))

    def test_publishable_key_accepted_and_csp(self):
        meta, origin = P.auth_config(self.cfg({"url": "https://abcdefghijklmnop.supabase.co", "key": "sb_publishable_" + "x" * 30}))
        self.assertEqual(origin, "https://abcdefghijklmnop.supabase.co")
        html = P.wrap("<title>t</title><header></header><script>1</script>", auth=(meta, origin))
        self.assertIn('name="auth-config"', html)
        self.assertIn("connect-src 'self' https://abcdefghijklmnop.supabase.co;", html)
        self.assertIn("&quot;url&quot;", html)

    def test_service_worker_is_same_origin_only(self):
        html = P.wrap("<title>t</title><header></header><script>1</script>")
        self.assertIn("worker-src 'self';", html)
        self.assertIn("default-src 'none'", html)
        sw = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web", "sw.js")).read()
        self.assertIn('r.method !== "GET" || r.mode !== "navigate"', sw)       # pages only, never data or sign-in calls
        self.assertIn("u.origin !== self.location.origin", sw)                  # never anything cross-origin
        self.assertIn('cache: "no-cache"', sw)                                  # always checks for a newer page, but an unchanged one is a 304
        self.assertNotIn('cache: "no-store"', sw)                               # (no-store re-downloaded the whole page on every visit)

    def test_anon_jwt_ok_service_role_refused(self):
        self.assertIsNotNone(P.auth_config(self.cfg({"url": "https://abcdefghijklmnop.supabase.co", "key": self.jwt("anon")}))[0])
        with self.assertRaises(RuntimeError):
            P.auth_config(self.cfg({"url": "https://abcdefghijklmnop.supabase.co", "key": self.jwt("service_role")}))
        with self.assertRaises(RuntimeError):
            P.auth_config(self.cfg({"url": "https://abcdefghijklmnop.supabase.co", "key": "sb_secret_" + "x" * 30}))

    def test_google_client_id(self):
        base = {"url": "https://abcdefghijklmnop.supabase.co", "key": "sb_publishable_" + "x" * 30}
        meta, _ = P.auth_config(self.cfg(dict(base, google_client_id="123456789-abcdefghijk.apps.googleusercontent.com")))
        self.assertIn("google_client_id", meta)
        meta, _ = P.auth_config(self.cfg(dict(base, google_client_id="GOCSPX-secret-looking-value")))
        self.assertNotIn("google_client_id", meta)

    def test_bad_url_ignored(self):
        self.assertEqual(P.auth_config(self.cfg({"url": "https://evil.example.com", "key": "sb_publishable_" + "x" * 30})), (None, None))
        self.assertEqual(P.auth_config(self.cfg({"url": "http://abcdefghijklmnop.supabase.co", "key": "sb_publishable_" + "x" * 30})), (None, None))

    def test_legal_pages(self):
        page = P.legal_page(*P.LEGAL["privacy.html"])
        self.assertNotIn("<script", page)
        self.assertNotIn("Contact", page)
        self.assertIn("Contact", P.legal_page(*P.LEGAL["terms.html"], contact="hello@example.com"))
        self.assertNotIn("Contact", P.legal_page(*P.LEGAL["terms.html"], contact="<x>@bad"))
