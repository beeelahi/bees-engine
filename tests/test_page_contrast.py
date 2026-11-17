"""Text colours on the dashboard stay readable (WCAG AA, 4.5:1) on the panels, in the dark and the light theme."""
import os
import re
import unittest

PAGE = os.path.join(os.path.dirname(__file__), "..", "web", "index.html")


def tokens(css, selector):
    block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css).group(1)
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9A-Fa-f]{6})", block))


def lum(h):
    def ch(c):
        c = int(c, 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(h[1:3]) + 0.7152 * ch(h[3:5]) + 0.0722 * ch(h[5:7])


def ratio(a, b):
    x, y = sorted((lum(a), lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


class ContrastTest(unittest.TestCase):
    def setUp(self):
        self.css = open(PAGE, encoding="utf-8").read()
        dark = tokens(self.css, ":root")
        light = dict(dark, **tokens(self.css, ':root[data-theme="light"]'))
        self.themes = {"dark": dark, "light": light}

    def test_text_tokens_readable_on_panels(self):
        for name, t in self.themes.items():
            for fg in ("fg", "muted", "cold-text"):
                for bg in ("bg", "panel", "panel-2"):
                    with self.subTest(theme=name, fg=fg, bg=bg):
                        self.assertGreaterEqual(ratio(t[fg], t[bg]), 4.5)

    def test_dim_text_uses_the_readable_token(self):
        self.assertRegex(self.css, r"\.cold \{ color: var\(--cold-text\); \}")

    def test_theme_color_meta_is_a_real_colour(self):     # a CSS variable is not valid in a meta tag
        self.assertRegex(self.css, r'<meta name="theme-color" content="#[0-9A-Fa-f]{6}">')


if __name__ == "__main__":
    unittest.main()
