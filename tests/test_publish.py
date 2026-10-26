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
