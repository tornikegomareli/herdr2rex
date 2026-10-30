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
        "public_pane_numbers": {"1": 1, "2": 2, "3": 3, "4": 4},
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


def renumber(node, offset):
    if "Pane" in node:
        return {"Pane": node["Pane"] + offset}
    split = node["Split"]
    return {"Split": {**split, "first": renumber(split["first"], offset), "second": renumber(split["second"], offset)}}


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
        self.claude = os.path.join(self.tmp.name, "claude")
        os.makedirs(os.path.join(self.claude, "sessions"))
        # Fake herdr: answers `pane list`, and acts stopped unless FAKE_HERDR_EXIT=0.
        herdr = os.path.join(self.tmp.name, "herdr")
        with open(herdr, "w") as f:
            f.write(f"#!/bin/sh\n[ \"$1\" = pane ] && echo '{json.dumps(HERDR_PANES)}' && exit 0\n"
                    "exit ${FAKE_HERDR_EXIT:-1}\n")
        os.chmod(herdr, 0o755)

    def tearDown(self):
        self.tmp.cleanup()
        subprocess.run([REX, "kill", LABEL], capture_output=True)

    def write_fixture(self, data):
        with open(self.fixture, "w") as f:
            json.dump(data, f)

    def run_tool(self, *args, herdr_file=None, check=True, **env):
        env = {**os.environ, "PATH": f"{self.tmp.name}:{os.environ['PATH']}",
               "XDG_STATE_HOME": self.state, "CLAUDE_CONFIG_DIR": self.claude, **env}
        return subprocess.run([os.path.join(HERE, "herdr2rex"), "--herdr-file", herdr_file or self.fixture, *args],
                              env=env, check=check, capture_output=True, text=True)

    def run_import(self):
        return self.run_tool().stdout

    def run_reverse(self, herdr_file=None):
        return self.run_tool("--reverse", LABEL, herdr_file=herdr_file).stdout

    def fake_claude(self, block, session_id):
        """Run a process in block and register it the way Claude Code registers its sessions."""
        rex("send", "-s", LABEL, "-b", block, "sleep 300\n")
        deadline = time.time() + 10
        while True:
            foreground = json.loads(rex("block", "call", "-s", LABEL, "-b", block, "process"))["foreground"]
            if foreground["name"] == "sleep":
                break
            self.assertLess(time.time(), deadline, "sleep never started")
            time.sleep(0.2)
        with open(os.path.join(self.claude, "sessions", f"{foreground['pid']}.json"), "w") as f:
            json.dump({"pid": foreground["pid"], "sessionId": session_id}, f)

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

    def test_rerun_after_herdr_restart_is_up_to_date(self):
        self.run_import()
        # A herdr restart gives every pane a new internal ID and keeps its public number.
        restarted = copy.deepcopy(FIXTURE)
        ws = restarted["workspaces"][0]
        ws["public_pane_numbers"] = {str(int(k) + 100): n for k, n in ws["public_pane_numbers"].items()}
        for tab in ws["tabs"]:
            tab["layout"] = renumber(tab["layout"], 100)
            tab["panes"] = {str(int(k) + 100): pane for k, pane in tab["panes"].items()}
        self.write_fixture(restarted)

        self.assertIn(f"{LABEL} is up to date", self.run_import())

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


    def test_reverse_replaces_the_herdr_workspace(self):
        self.run_import()
        root = inspect()["windows"][0]["layers"][0]["layout"]["split"]
        self.fake_claude(root["after"]["block_id"], "rex-claude-1")

        out = self.run_reverse()
        self.assertIn(f"replaced {LABEL}", out)
        self.assertIn(f"rex kill {LABEL}", out)
        self.assertTrue(os.path.exists(self.fixture + ".bak"))
        with open(self.fixture) as f:
            (ws,) = json.load(f)["workspaces"]
        self.assertEqual(ws["id"], "w1")
        self.assertEqual(ws["custom_name"], LABEL)
        self.assertEqual(sorted(ws["public_pane_numbers"].values()), [1, 2, 3, 4])
        main, second = ws["tabs"]
        self.assertEqual(main["custom_name"], "main")
        self.assertIsNone(second["custom_name"])
        split = main["layout"]["Split"]
        self.assertEqual(split["direction"], "Horizontal")
        self.assertAlmostEqual(split["ratio"], 0.6, places=5)
        self.assertEqual(split["first"]["Split"]["direction"], "Vertical")
        self.assertAlmostEqual(split["first"]["Split"]["ratio"], 0.7, places=5)
        pane = main["panes"][str(split["second"]["Pane"])]
        self.assertEqual(pane["cwd"], os.path.expanduser("~"))
        self.assertEqual(pane["agent_session"], session("claude", "rex-claude-1"))

        # The round trip keeps public pane numbers, so importing it again changes nothing.
        self.assertIn(f"{LABEL} is up to date", self.run_import())

    def test_reverse_exports_into_an_empty_herdr_file(self):
        self.run_import()
        herdr_file = os.path.join(self.tmp.name, "new", "session.json")
        os.makedirs(os.path.dirname(herdr_file))

        self.assertIn(f"exported {LABEL}", self.run_reverse(herdr_file))
        with open(herdr_file) as f:
            herdr = json.load(f)
        self.assertEqual(herdr["version"], 3)
        self.assertEqual([ws["custom_name"] for ws in herdr["workspaces"]], [LABEL])

    def test_reverse_refuses_while_herdr_runs(self):
        self.run_import()
        result = self.run_tool("--reverse", LABEL, check=False, FAKE_HERDR_EXIT="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("herdr server stop", result.stderr)


if __name__ == "__main__":
    unittest.main()
