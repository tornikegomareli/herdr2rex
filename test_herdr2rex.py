"""End-to-end test. Needs a running Rex server. Creates and kills one Rex session."""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REX = shutil.which("rex") or os.path.expanduser(
    "~/Library/Application Support/com.superlogical.rex/RexServer/Versions/Current/Rex Server.app/Contents/MacOS/rex"
)
LABEL = "zz-herdr2rex-test"
CLAUDE_ID = "00000000-0000-0000-0000-000000000000"


def session(agent, value):
    return {"source": f"herdr:{agent}", "agent": agent, "kind": "id", "value": value}


FIXTURE = {
    "version": 3,
    "workspaces": [{
        "id": "w1",
        "custom_name": LABEL,
        "identity_cwd": "/tmp",
        "tabs": [
            {
                "custom_name": "main",
                "layout": {"Split": {"direction": "Horizontal", "ratio": 0.6,
                                     "first": {"Split": {"direction": "Vertical", "ratio": 0.7,
                                                         "first": {"Pane": 1}, "second": {"Pane": 2}}},
                                     "second": {"Pane": 3}}},
                "panes": {
                    "1": {"cwd": "/tmp", "agent_session": session("claude", CLAUDE_ID)},
                    "2": {"cwd": "/tmp", "agent_session": session("copilot", "copilot-1")},
                    "3": {"cwd": os.path.expanduser("~"), "agent_session": session("amp", "amp-1")},
                },
            },
            {"custom_name": None, "layout": {"Pane": 4}, "panes": {"4": {"cwd": "/tmp"}}},
        ],
    }],
}


def rex(*args):
    return subprocess.run([REX, *args], check=True, capture_output=True, text=True).stdout


class ImportTest(unittest.TestCase):
    def setUp(self):
        fd, self.fixture = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(FIXTURE, f)

    def tearDown(self):
        os.remove(self.fixture)
        subprocess.run([REX, "kill", LABEL], capture_output=True)

    def run_import(self):
        return subprocess.run([os.path.join(HERE, "herdr2rex"), "--from", self.fixture],
                              check=True, capture_output=True, text=True).stdout

    def wait_for(self, block, text):
        deadline = time.time() + 10
        while text not in rex("capture", "-b", block):
            self.assertLess(time.time(), deadline, f"{text!r} never reached the shell")
            time.sleep(0.5)

    def test_import(self):
        self.assertIn(f"imported {LABEL}", self.run_import())
        inspected = json.loads(rex("session", "inspect", LABEL, "--json"))
        main, second = inspected["windows"]

        self.assertEqual(main["label"], "main")
        layer = main["layers"][0]
        root = layer["layout"]["split"]
        self.assertEqual(root["direction"], "horizontal")
        self.assertAlmostEqual(root["ratio"], 0.6)
        inner = root["before"]["split"]
        self.assertEqual(inner["direction"], "vertical")
        self.assertAlmostEqual(inner["ratio"], 0.7)
        self.assertIn("block_id", root["after"])
        self.assertEqual(len(layer["blocks"]), 3)
        self.assertIn("block_id", second["layers"][0]["layout"])

        self.wait_for(inner["before"]["block_id"], f"claude --resume {CLAUDE_ID}")
        self.wait_for(inner["after"]["block_id"], "copilot --resume=copilot-1")
        self.assertNotIn("amp-1", rex("capture", "-b", root["after"]["block_id"]))

        self.assertIn(f"skip {LABEL}", self.run_import())


if __name__ == "__main__":
    unittest.main()
