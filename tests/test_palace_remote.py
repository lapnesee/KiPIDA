import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from palace_remote import (
    PalaceRemoteClient, PalaceRemoteConnection, _parse_palace_config,
    _quote_remote_path,
)


class _FakePalaceClient(PalaceRemoteClient):
    def __init__(self, connection, dry_run_code=0, solve_code=0, solve_status="COMPLETED"):
        super().__init__(connection)
        self.ssh_path = "ssh"
        self.scp_path = "scp"
        self.commands = []
        self.dry_run_code = dry_run_code
        self.solve_code = solve_code
        self.solve_status = solve_status

    def probe(self):
        return "Palace test wrapper"

    def _capture(self, command, timeout, cwd=None):
        self.commands.append(list(map(str, command)))
        command_text = " ".join(map(str, command))
        if "--dry-run" in command_text:
            return subprocess.CompletedProcess(command, self.dry_run_code, "dry run", "invalid")
        if (command and str(command[0]).endswith("scp")
                and str(command[-2]).startswith("solver@")):
            destination = Path(command[-1])
            (destination / "postpro").mkdir(parents=True, exist_ok=True)
            (destination / "postpro" / "port-S.csv").write_text("f,S11\n1,-3\n")
            (destination / "postpro" / "case_resolved.json").write_text("{}")
        return subprocess.CompletedProcess(command, 0, "", "")

    def _monitor(self, command, timeout, log_path):
        self.commands.append(list(map(str, command)))
        Path(log_path).write_text("Palace complete\n", encoding="utf-8")
        return self.solve_code, self.solve_status


class PalaceRemoteTests(unittest.TestCase):
    def connection(self, **changes):
        values = dict(
            host="192.168.1.40", username="solver", port=22,
            remote_root="~/kipida-palace", executable="palace",
            mpi_processes=4, host_key_policy="STRICT",
            connect_timeout_s=5, run_timeout_s=60,
        )
        values.update(changes)
        return PalaceRemoteConnection(**values)

    def test_connection_validation_rejects_command_injection(self):
        for field, value in (
            ("host", "server;shutdown"),
            ("username", "solver user"),
            ("remote_root", "~/jobs;rm"),
            ("executable", "palace && whoami"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.connection(**{field: value}).validate()

    def test_ssh_probe_command_is_non_interactive_and_strict(self):
        client = _FakePalaceClient(self.connection())
        command = client._ssh_command("palace", "--help")
        rendered = " ".join(command)
        self.assertIn("BatchMode=yes", rendered)
        self.assertIn("StrictHostKeyChecking=yes", rendered)
        self.assertIn("solver@192.168.1.40", rendered)

    def test_standard_config_metadata_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.json"
            path.write_text(json.dumps({"Problem": {"Type": "Driven", "Output": "out"}}))
            self.assertEqual(_parse_palace_config(path)[:2], ("Driven", "out"))

    def test_home_remote_paths_expand_without_exposing_shell_metacharacters(self):
        self.assertEqual(_quote_remote_path("~/kipida-palace/job"), '"$HOME"/kipida-palace/job')

    def test_remote_shell_expression_is_preserved_as_one_quoted_argument(self):
        client = _FakePalaceClient(self.connection())
        command = client._ssh_shell_command('cd "$HOME"/job/input && exec palace --dry-run case.json')
        rendered = " ".join(command)
        self.assertIn("sh -lc", rendered)
        self.assertIn("'cd \"$HOME\"/job/input && exec palace --dry-run case.json'", rendered)

    def test_remote_project_is_dry_run_solved_and_retrieved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "project" / "case.json"
            config.parent.mkdir()
            config.write_text(json.dumps({
                "Problem": {"Type": "Driven", "Output": "postpro"},
                "Model": {"Mesh": "mesh.msh"},
            }))
            (config.parent / "mesh.msh").write_text("mesh")
            client = _FakePalaceClient(self.connection())
            result = client.run_project(config, root / "results")
        self.assertEqual(result.status, "SOLVED_REMOTE")
        self.assertTrue(result.dry_run_passed)
        self.assertEqual(result.problem_type, "Driven")
        self.assertEqual(len(result.csv_files), 1)
        self.assertTrue(result.resolved_config_path.endswith("case_resolved.json"))
        rendered = "\n".join(" ".join(item) for item in client.commands)
        self.assertIn("--dry-run", rendered)
        self.assertIn("/input && exec palace -serial --dry-run case.json", rendered)
        self.assertIn("-np 4", rendered)

    def test_remote_validation_failure_never_starts_solver(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "case.json"
            config.write_text(json.dumps({"Problem": {"Type": "Electrostatic"}}))
            client = _FakePalaceClient(self.connection(), dry_run_code=2)
            result = client.run_project(config, root / "results")
        self.assertEqual(result.status, "VALIDATION_FAILED")
        self.assertFalse(result.dry_run_passed)
        rendered = "\n".join(" ".join(item) for item in client.commands)
        self.assertIn("sh -lc", rendered)
        self.assertNotIn("-np 4", rendered)


if __name__ == "__main__":
    unittest.main()
