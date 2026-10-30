"""End-to-end tests. They need a running Rex server. Each test creates and kills one Rex session."""
import copy
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
TITLE = "Fix the login bug"
# What the fake `herdr pane list` prints: a live title for the first agent pane only.
HERDR_PANES = {"result": {"panes": [{"agent_session": {"value": CLAUDE_ID}, "terminal_title_stripped": TITLE}]}}


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


def inspect():
    return json.loads(rex("session", "inspect", LABEL, "--json"))


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = os.path.join(self.tmp.name, "session.json")
        self.state = os.path.join(self.tmp.name, "state")
        self.write_fixture(FIXTURE)
        herdr = os.path.join(self.tmp.name, "herdr")
        with open(herdr, "w") as f:
            f.write(f"#!/bin/sh\necho '{json.dumps(HERDR_PANES)}'\n")
        os.chmod(herdr, 0o755)

    def tearDown(self):
        self.tmp.cleanup()
        subprocess.run([REX, "kill", LABEL], capture_output=True)

    def write_fixture(self, data):
        with open(self.fixture, "w") as f:
            json.dump(data, f)

    def run_import(self):
        env = {**os.environ, "PATH": f"{self.tmp.name}:{os.environ['PATH']}", "XDG_STATE_HOME": self.state}
        return subprocess.run([os.path.join(HERE, "herdr2rex"), "--from", self.fixture],
                              env=env, check=True, capture_output=True, text=True).stdout

    def wait_for(self, block, text):
        deadline = time.time() + 10
        while text not in rex("capture", "-s", LABEL, "-b", block):
            self.assertLess(time.time(), deadline, f"{text!r} never reached the shell")
            time.sleep(0.5)

    def test_import(self):
        self.assertIn(f"imported {LABEL}", self.run_import())
        main, second = inspect()["windows"]

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
        self.assertNotIn("amp-1", rex("capture", "-s", LABEL, "-b", root["after"]["block_id"]))

        labels = {b["block_id"]: b["label"] for b in layer["blocks"]}
        self.assertEqual(labels[inner["before"]["block_id"]], TITLE)
        self.assertEqual(labels[inner["after"]["block_id"]], "shell")

        self.assertIn(f"{LABEL} is up to date", self.run_import())

    def test_rerun_adds_new_panes_and_tabs(self):
        self.run_import()
        changed = copy.deepcopy(FIXTURE)
        tabs = changed["workspaces"][0]["tabs"]
        tabs[1]["layout"] = {"Split": {"direction": "Vertical", "ratio": 0.6, "first": {"Pane": 4}, "second": {"Pane": 5}}}
        tabs[1]["panes"]["5"] = {"cwd": "/tmp"}
        tabs.append({"custom_name": "logs", "layout": {"Pane": 6}, "panes": {"6": {"cwd": "/tmp"}}})
        self.write_fixture(changed)

        self.assertIn(f"added 2 panes to {LABEL}", self.run_import())
        windows = inspect()["windows"]
        self.assertEqual(len(windows), 3)
        split = windows[1]["layers"][0]["layout"]["split"]
        self.assertEqual(split["direction"], "vertical")
        self.assertAlmostEqual(split["ratio"], 0.6)
        self.assertEqual(windows[2]["label"], "logs")

    def test_rerun_restores_a_closed_block(self):
        self.run_import()
        inner = inspect()["windows"][0]["layers"][0]["layout"]["split"]["before"]["split"]
        rex("block", "close", "-s", LABEL, inner["before"]["block_id"])

        self.assertIn(f"added 1 pane to {LABEL}", self.run_import())
        restored = inspect()["windows"][0]["layers"][0]["layout"]["split"]["before"]["split"]
        self.assertEqual(restored["direction"], "vertical")
        self.assertAlmostEqual(restored["ratio"], 0.7)
        self.assertEqual(restored["after"]["block_id"], inner["after"]["block_id"])

    def test_skips_a_session_it_did_not_create(self):
        self.run_import()
        self.state = os.path.join(self.tmp.name, "other-state")
        self.assertIn(f"skip {LABEL}", self.run_import())


if __name__ == "__main__":
    unittest.main()
