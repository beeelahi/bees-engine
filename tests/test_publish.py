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

    def test_no_publish_switch(self):
        open(self.r + "/.no_publish", "w").close()
        self.assertFalse(P.run(lambda *a: None))


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
