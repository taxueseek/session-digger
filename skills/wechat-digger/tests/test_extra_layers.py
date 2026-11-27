#!/usr/bin/env python3
"""Zero-risk extra layers + history FTS gap-fill."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from extra_layers import decrypted_root, list_friend_requests, list_payments, list_voice  # noqa: E402


class TestExtraLayersLive(unittest.TestCase):
    @unittest.skipUnless(decrypted_root() and (decrypted_root() / "message" / "media_0.db").exists(), "media_0 missing")
    def test_voice_list_has_span(self):
        out = list_voice(since="2022-01-01", until="2022-12-31", limit=5)
        self.assertNotIn("error", out)
        self.assertIn("voice", out)
        for item in out["voice"]:
            self.assertNotIn("voice_data", item)
            self.assertEqual(item["msg_type"], "voice")

    @unittest.skipUnless(decrypted_root() and (decrypted_root() / "general" / "general.db").exists(), "general.db missing")
    def test_payments_and_requests_shape(self):
        pay = list_payments(kind="all", limit=10)
        self.assertNotIn("error", pay)
        self.assertIn("payments", pay)
        req = list_friend_requests(limit=5)
        self.assertNotIn("error", req)
        for item in req["requests"]:
            self.assertNotIn("ticket_", item)
            self.assertLessEqual(len(item.get("text") or ""), 200)


if __name__ == "__main__":
    unittest.main()
