import unittest
from pathlib import Path

import modal_app as m


class LogoutCsrfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(m.__file__).read_text(encoding="utf-8")
        cls.block = cls.source.split('@web_app.post("/logout")', 1)[1].split('@web_app.get("/")', 1)[0]

    def test_logout_is_post_only_and_home_uses_post_form(self):
        self.assertIn('@web_app.post("/logout")', self.source)
        self.assertNotIn('@web_app.get("/logout")', self.source)
        self.assertIn("method='post' action='/logout'", m.HOME_HTML)

    def test_cross_origin_logout_is_rejected_before_session_mutation(self):
        self.assertIn("if not safe_origin(request):", self.block)
        self.assertLess(self.block.index("if not safe_origin(request):"), self.block.index("close_session(request)"))

    def test_same_origin_logout_revokes_session_redirects_and_deletes_cookie(self):
        self.assertIn("if not close_session(request):", self.block)
        self.assertIn('RedirectResponse("/login", status_code=303)', self.block)
        self.assertIn('response.delete_cookie("raiw_session", path="/")', self.block)


if __name__ == "__main__":
    unittest.main()
