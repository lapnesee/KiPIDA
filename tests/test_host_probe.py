import unittest
from types import SimpleNamespace

from host_probe import describe_host, host_environment_variable_names, process_ancestry


class HostProbeTests(unittest.TestCase):
    def test_token_value_never_reaches_the_log(self):
        # The IPC token authenticates the session; the probe writes to a
        # world-readable temp file, so only variable names may appear.
        environ = {
            "KICAD_API_TOKEN": "s3cr3t-token",
            "KICAD_API_SOCKET": r"\\.\pipe\kicad",
            "ZEO_SUPABASE_URL": "https://example.invalid",
            "PATH": "C:/bin",
        }
        lines = describe_host(environ=environ, ancestry=[])
        text = "\n".join(lines)
        self.assertNotIn("s3cr3t-token", text)
        self.assertNotIn("example.invalid", text)
        self.assertEqual(
            host_environment_variable_names(environ),
            ["KICAD_API_SOCKET", "KICAD_API_TOKEN", "ZEO_SUPABASE_URL"],
        )

    def test_missing_or_failing_host_apis_are_reported_not_raised(self):
        # An older kipy lacks get_kicad_binary_path, and a dead connection
        # raises on every call; either must yield a readable line, since the
        # probe also runs after a failed connection.
        def raises(*_args):
            raise ConnectionError("gone")

        client = SimpleNamespace(get_version=raises)
        lines = describe_host(client=client, board=None, project=None, environ={}, ancestry=[])
        self.assertIn("IPC version: unavailable", lines)
        self.assertIn("Host kicad-cli: unavailable", lines)
        self.assertIn("Process ancestry: unavailable", lines)

    def test_reports_what_the_host_supplied(self):
        client = SimpleNamespace(
            get_version=lambda: SimpleNamespace(full_version="10.0.3 (10.0.3)"),
            get_kicad_binary_path=lambda name: rf"C:\Program Files\Zeo\v0.3.0\bin\{name}.exe",
        )
        document = SimpleNamespace(
            board_filename="p02.kicad_pcb",
            project=SimpleNamespace(name="p02", path=r"D:\proj\p02.kicad_pro"),
        )
        lines = describe_host(
            client=client, board=SimpleNamespace(document=document),
            project=SimpleNamespace(name="p02", path=r"D:\proj\p02.kicad_pro"),
            environ={}, ancestry=[r"C:\Python\python.exe", r"C:\Program Files\Zeo\v0.3.0\bin\Zeo.exe"],
        )
        text = "\n".join(lines)
        self.assertIn("IPC version: 10.0.3 (10.0.3)", text)
        self.assertIn(r"Host kicad-cli: C:\Program Files\Zeo\v0.3.0\bin\kicad-cli.exe", text)
        self.assertIn("board_filename=p02.kicad_pcb", text)
        self.assertIn(r"Zeo.exe", text)

    def test_ancestry_walk_runs_on_this_platform(self):
        # The platform-specific walk is the part most likely to break silently,
        # because every failure inside it is swallowed into an empty list.
        import os

        chain = process_ancestry()
        self.assertIsInstance(chain, list)
        self.assertTrue(all(isinstance(item, str) and item for item in chain))
        if os.getppid() > 1:  # a container init as direct parent has nothing above it
            self.assertGreater(len(chain), 0)


if __name__ == "__main__":
    unittest.main()
