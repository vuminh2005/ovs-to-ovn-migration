"""Execution boundaries that a source-folder reorganization can break."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.support import ROOT
from _paths import SCRIPT_FILES, script_path
import reset_local_probes as probes


class SourceLayoutTests(unittest.TestCase):
    def test_registry_covers_every_helper_once(self):
        actual = list((ROOT / "scripts").glob("*/*.py"))
        self.assertEqual(len({p.name for p in actual}), len(actual))
        self.assertEqual(
            {p.relative_to(ROOT / "scripts").as_posix() for p in actual},
            set(SCRIPT_FILES.values()),
        )
        self.assertEqual({p.name for p in actual}, set(SCRIPT_FILES))

    def test_all_grouped_modules_import_from_an_unrelated_directory(self):
        program = (
            "import importlib,json,sys\n"
            "sys.path.insert(0,sys.argv[1])\n"
            "from _paths import SCRIPT_FILES,configure\n"
            "configure()\n"
            "for name in SCRIPT_FILES: importlib.import_module(name[:-3])\n"
            "print(json.dumps(sorted(SCRIPT_FILES)))\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-I", "-c", program, str(ROOT / "scripts")],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=20,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), sorted(SCRIPT_FILES))

    def test_remote_and_guest_bundles_import_without_the_source_checkout(self):
        bundles = (
            ("dataplane_capture", ("dataplane_capture.py", "run_layout.py")),
            (
                "ns_diagnostics",
                ("ns_diagnostics.py", "dataplane_capture.py", "run_layout.py"),
            ),
            ("migration_diagnostics", ("migration_diagnostics.py", "run_layout.py")),
            (
                "ns_probe",
                ("ns_probe.py", "ns_icmp.py", "ns_log_transfer.py", "run_layout.py"),
            ),
            ("cutover_preparation", ("cutover_preparation.py",)),
            ("provider_associations", ("provider_associations.py",)),
        )
        for module, files in bundles:
            with (
                self.subTest(module=module),
                tempfile.TemporaryDirectory() as directory,
            ):
                for name in files:
                    shutil.copyfile(script_path(name), Path(directory) / name)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        "import importlib,sys;sys.path.insert(0,sys.argv[1]);importlib.import_module(sys.argv[2])",
                        directory,
                        module,
                    ],
                    cwd=directory,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_reset_recognizes_the_same_checkout_old_probe_path_only(self):
        root = Path("/private-backups/run-one")
        config = root / "ns/snat-server.json"
        for source in (probes.PROBE_SCRIPT, probes.LEGACY_PROBE_SCRIPT):
            args = ["python3", str(source), "serve", str(config)]
            with (
                self.subTest(source=source),
                patch.object(probes, "process_command", return_value=args),
            ):
                self.assertEqual(
                    probes.orphan_config(args, root.parent),
                    (root, "snat-server", config),
                )
                probes.command_owned(123, "snat-server", config, exact_script=True)
        args = [
            "python3",
            "/another-checkout/scripts/ns_probe.py",
            "serve",
            str(config),
        ]
        self.assertIsNone(probes.orphan_config(args, root.parent))
        with patch.object(probes, "process_command", return_value=args):
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                probes.command_owned(123, "snat-server", config, exact_script=True)

    def test_failed_recovery_still_invokes_the_real_diagnostics_helper(self):
        # Ansible initializes process-wide display/capture state on import.
        # Keep that state out of the parent pytest process.
        program = (
            'import sys;sys.path.insert(0,sys.argv[1]);'
            'from tests.common.test_project_layout import SourceLayoutTests;'
            'SourceLayoutTests()._check_failed_recovery_callback();print("PASS")'
        )
        result = subprocess.run([sys.executable, '-I', '-c', program, str(ROOT)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(result.stdout.strip(), 'PASS')

    def _check_failed_recovery_callback(self):
        spec = importlib.util.spec_from_file_location(
            "layout_timing_callback", ROOT / "plugins/callback/migration_task_timing.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entries = (
            "migrate-to-ovn.yml",
            "archive/playbooks/retry-validation-preparation.yml",
            "archive/playbooks/retry-ns-diagnostics-preparation.yml",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "diagnostics-config.json").write_text("{}")
            stats = SimpleNamespace(
                processed={"host": True}, summarize=lambda host: {"failures": 1}
            )
            for entry in entries:
                callback = module.CallbackModule()
                callback.root, callback.playbook = root, str(ROOT / entry)
                with (
                    self.subTest(entry=entry),
                    patch.object(module.subprocess, "run") as run,
                    patch.dict(os.environ, OVS_TO_OVN_DIAGNOSTICS_FINALIZING="0"),
                ):
                    callback._collect_failed_run_diagnostics(stats)
                    run.assert_called_once()
                    command = run.call_args.args[0]
                    self.assertEqual(
                        Path(command[1]), script_path("diagnostics_control.py")
                    )
                    self.assertTrue(Path(command[1]).is_file())
                    self.assertEqual(command[2:], ["abort", str(root)])

            for path in (ROOT/'archive/playbooks').glob('resume-*.yml'):
                callback = module.CallbackModule()
                callback.root, callback.playbook = root, str(path)
                with self.subTest(archived=path.name), patch.object(module.subprocess, "run") as run:
                    callback._collect_failed_run_diagnostics(stats)
                    run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
