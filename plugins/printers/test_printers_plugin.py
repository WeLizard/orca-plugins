import ast
import importlib.util
import base64
import csv
import hashlib
import io
import inspect
import json
import pathlib
import re
import sys
import tempfile
import threading
import tomllib
import types
import unittest
import urllib.error
import zipfile
from unittest import mock


PLUGIN_PATH = pathlib.Path(__file__).with_name("printers_plugin.py")
BUILD_PATH = pathlib.Path(__file__).with_name("build_package.py")
LOCALE_VALIDATOR_PATH = pathlib.Path(__file__).with_name("validate_locales.py")


class _ExecutionResult:
    @staticmethod
    def success(message):
        return ("success", message)

    @staticmethod
    def skipped(message):
        return ("skipped", message)

    @staticmethod
    def failure(kind, message):
        return ("failure", kind, message)


def _load_plugin(*, lifecycle=False, pages=False, registered=None):
    def capability_base(name, *, page=False):
        namespace = {}
        if lifecycle:
            namespace.update({
                "on_load": lambda self: None,
                "on_unload": lambda self: None,
            })
        if page:
            namespace["post_message"] = lambda self, data: None
        return type(name, (object,), namespace) if namespace else object

    fake_orca = types.ModuleType("orca")
    fake_orca.base = object
    fake_orca.plugin = lambda cls: cls
    fake_orca.register_capability = (
        registered.append if registered is not None else lambda capability: None
    )
    fake_orca.ExecutionResult = _ExecutionResult
    fake_orca.PluginResult = types.SimpleNamespace(RecoverableError="recoverable")
    fake_orca.script = types.SimpleNamespace(
        ScriptPluginCapabilityBase=capability_base("ScriptCapabilityBase")
    )
    if pages:
        fake_orca.pages = types.SimpleNamespace(
            PagesPluginCapabilityBase=capability_base(
                "PagesCapabilityBase",
                page=True,
            )
        )
    fake_orca.host = types.SimpleNamespace(ui=types.SimpleNamespace())
    fake_orca.slicing = types.SimpleNamespace(
        SlicingPipelineCapabilityBase=capability_base("SlicingCapabilityBase"),
        Step=types.SimpleNamespace(psGCodePostProcess="post-process"),
    )
    sys.modules["orca"] = fake_orca

    module_name = "printers_plugin_under_test_%s" % id(fake_orca)
    spec = importlib.util.spec_from_file_location(module_name, PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PrintersOutboxTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp_dir.name)
        self.plugin.OUTBOX_DIR = str(root / "outbox")
        self.plugin.OUTBOX_INDEX = str(root / "outbox_index.json")
        self.gcode = root / "fresh.gcode"
        self.gcode.write_text(
            "; printer_settings_id = Voron 2.4 350\n"
            "; printer_model = Voron 2.4\n"
            "G28\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def execute(self, host):
        ctx = types.SimpleNamespace(
            step="post-process",
            gcode_path=str(self.gcode),
            output_name="fresh.gcode",
            host=host,
        )
        return self.plugin.PrintersOutbox().execute(ctx)

    def test_file_target_is_queued(self):
        result = self.execute("File")

        self.assertEqual(result[0], "success")
        record = self.plugin.load_outbox_index()[0]
        self.assertFalse(record["host_handled"])
        self.assertEqual(record["host"], "File")

    def test_file_target_is_case_insensitive(self):
        result = self.execute(" file ")

        self.assertEqual(result[0], "success")
        self.assertEqual(len(self.plugin.load_outbox_index()), 1)

    def test_legacy_empty_target_is_queued(self):
        result = self.execute("")

        self.assertEqual(result[0], "success")
        self.assertEqual(len(self.plugin.load_outbox_index()), 1)

    def test_network_upload_is_also_queued(self):
        result = self.execute("OctoPrint")

        self.assertEqual(result[0], "success")
        record = self.plugin.load_outbox_index()[0]
        self.assertTrue(record["host_handled"])
        self.assertEqual(record["host"], "OctoPrint")

    def test_hub_version_matches_wheel_project(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        match = re.search(r'^# version = "([0-9]+\.[0-9]+\.[0-9]+)"$', source, re.MULTILINE)
        self.assertIsNotNone(match)
        project = tomllib.loads(PLUGIN_PATH.with_name("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["dynamic"], ["version"])
        self.assertEqual(
            project["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "printers_plugin.PLUGIN_VERSION"},
        )
        self.assertEqual(project["tool"]["setuptools"]["py-modules"], ["printers_plugin"])
        self.assertNotIn("packages", project["tool"]["setuptools"])
        self.assertEqual(match.group(1), self.plugin.PLUGIN_VERSION)
        self.assertIn('# name = "Print Farm"', source)
        self.assertEqual(self.plugin.PrintersDashboard().get_name(), "Print Farm")

    def test_package_builder_stages_source_and_icon(self):
        spec = importlib.util.spec_from_file_location("printers_build_under_test", BUILD_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        with tempfile.TemporaryDirectory() as temp:
            package = builder.build(pathlib.Path(temp), wheel=False)

            self.assertEqual(package.name, "printers-0.0.6")
            staged_source = (package / "printers_plugin.py").read_text(encoding="utf-8")
            self.assertNotIn(builder.EMBEDDED_UI_COPY_TOKEN, staged_source)
            self.assertIn('_EMBEDDED_UI_COPY = {"ca":', staged_source)
            ast.parse(staged_source)
            self.assertEqual(
                (package / "printers.svg").read_bytes(),
                PLUGIN_PATH.with_name("printers.svg").read_bytes(),
            )
            self.assertEqual(
                (package / "description.md").read_bytes(),
                PLUGIN_PATH.with_name("description.md").read_bytes(),
            )
            self.assertEqual(
                {path.name for path in (package / "printers_locales").glob("*.json")},
                {"%s.json" % locale for locale in self.plugin.ORCA_UI_LOCALES},
            )
            metadata = json.loads(
                (package / "package-metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["locales"], sorted(self.plugin.ORCA_UI_LOCALES))

    def test_wheel_metadata_uses_lf_and_updates_record_hash(self):
        spec = importlib.util.spec_from_file_location("printers_build_under_test", BUILD_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        with tempfile.TemporaryDirectory() as temp:
            wheel = pathlib.Path(temp) / "printers-0.0.6-py3-none-any.whl"
            metadata_path = "printers-0.0.6.dist-info/METADATA"
            record_path = "printers-0.0.6.dist-info/RECORD"
            metadata = b"Metadata-Version: 2.4\r\nName: printers\r\nVersion: 0.0.6\r\n\r\n"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(metadata_path, metadata)
                archive.writestr(
                    record_path,
                    f"{metadata_path},old,0\r\n{record_path},,\r\n",
                )

            builder._normalize_wheel_metadata(wheel)

            with zipfile.ZipFile(wheel) as archive:
                normalized = archive.read(metadata_path)
                rows = list(csv.reader(io.StringIO(
                    archive.read(record_path).decode("utf-8"),
                    newline="",
                )))
            self.assertNotIn(b"\r", normalized)
            expected_digest = base64.urlsafe_b64encode(
                hashlib.sha256(normalized).digest()
            ).rstrip(b"=").decode("ascii")
            metadata_row = next(row for row in rows if row[0] == metadata_path)
            self.assertEqual(metadata_row[1], f"sha256={expected_digest}")
            self.assertEqual(metadata_row[2], str(len(normalized)))

    def test_release_notes_use_only_current_changelog_section(self):
        spec = importlib.util.spec_from_file_location("printers_build_under_test", BUILD_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        notes = builder.render_release_notes("0.0.6")

        self.assertIn("## Print Farm 0.0.6", notes)
        self.assertIn("connected GitHub release workflow", notes)
        self.assertNotIn("local print history", notes)
        self.assertNotIn("Made add, remove, refresh", notes)


class PluginStorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def test_host_storage_migrates_state_without_deleting_legacy(self):
        plugin = _load_plugin(lifecycle=True, pages=True)
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            legacy = root / "orca_plugins" / "printers"
            storage = root / "plugin_data" / "printers"
            legacy.mkdir(parents=True)
            (legacy / "printers.json").write_text('[{"id":1}]', encoding="utf-8")
            (legacy / "outbox_index.json").write_text(
                '[{"file":"fixture.gcode"}]', encoding="utf-8"
            )
            (legacy / "print_history.json").write_text(
                '[{"id":"history-1","started_at":1}]', encoding="utf-8"
            )
            (legacy / "outbox").mkdir()
            (legacy / "outbox" / "fixture.gcode").write_text("G28", encoding="utf-8")

            in_callback = False

            def host_storage():
                if not in_callback:
                    raise RuntimeError("storage requires a plugin callback")
                return str(storage)

            plugin_host = types.SimpleNamespace(storage=mock.Mock(side_effect=host_storage))
            with mock.patch.object(plugin, "PLUGIN_DIR", str(legacy)), \
                 mock.patch.object(plugin, "PLUGIN_STORAGE_DIR", str(legacy)), \
                 mock.patch.object(plugin, "PRINTERS_FILE", str(legacy / "printers.json")), \
                 mock.patch.object(plugin, "PRINT_HISTORY_FILE", str(legacy / "print_history.json")), \
                 mock.patch.object(plugin, "OUTBOX_DIR", str(legacy / "outbox")), \
                 mock.patch.object(plugin, "OUTBOX_INDEX", str(legacy / "outbox_index.json")), \
                 mock.patch.object(plugin.orca.host, "plugin", plugin_host, create=True):
                plugin.PrintersPlugin().register_capabilities()
                plugin_host.storage.assert_not_called()

                in_callback = True
                dashboard = plugin.PrintersDashboard()
                dashboard.on_load()

                self.assertEqual(plugin.PLUGIN_STORAGE_DIR, str(storage))
                self.assertEqual(plugin.PRINTERS_FILE, str(storage / "printers.json"))
                self.assertEqual(
                    plugin.PRINT_HISTORY_FILE,
                    str(storage / "print_history.json"),
                )
                self.assertEqual(plugin.OUTBOX_DIR, str(storage / "outbox"))
                self.assertEqual(
                    (storage / "printers.json").read_text(encoding="utf-8"),
                    '[{"id":1}]',
                )
                self.assertEqual(
                    (storage / "outbox" / "fixture.gcode").read_text(encoding="utf-8"),
                    "G28",
                )
                self.assertEqual(
                    json.loads((storage / "print_history.json").read_text(encoding="utf-8"))[0]["id"],
                    "history-1",
                )
                dashboard.on_unload()

            self.assertTrue((legacy / "printers.json").exists())
            self.assertTrue((legacy / "outbox" / "fixture.gcode").exists())

    def test_old_host_uses_stable_storage_outside_replaceable_plugin_dir(self):
        plugin = _load_plugin()
        with tempfile.TemporaryDirectory() as temp:
            data_root = pathlib.Path(temp)
            legacy = data_root / "orca_plugins" / "printers"
            legacy.mkdir(parents=True)
            (legacy / "printers.json").write_text('[{"id":7}]', encoding="utf-8")
            expected = data_root / ".print-farm" / "orca-plugin"

            with mock.patch.object(plugin, "PLUGIN_DIR", str(legacy)), \
                 mock.patch.object(
                     plugin.orca.host,
                     "plugin",
                     types.SimpleNamespace(),
                     create=True,
                 ):
                self.assertTrue(plugin.configure_plugin_storage())

            self.assertEqual(plugin.PLUGIN_STORAGE_DIR, str(expected))
            self.assertEqual(
                (expected / "printers.json").read_text(encoding="utf-8"),
                '[{"id":7}]',
            )

    def test_single_file_install_materializes_the_tab_icon(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            missing = root / "not-packaged.svg"
            with mock.patch.object(self.plugin, "PACKAGED_ICON_PATH", str(missing)), \
                 mock.patch.object(self.plugin, "ICON_PATH", str(missing)), \
                 mock.patch.object(self.plugin, "PLUGIN_STORAGE_DIR", str(root)):
                icon = self.plugin.ensure_icon()

            self.assertEqual(icon, str(root / "printers.svg"))
            self.assertEqual((root / "printers.svg").read_bytes(), self.plugin._ICON_SVG)

    def test_repeated_orca_picker_choice_upserts_one_printer(self):
        dashboard = self.plugin.PrintersDashboard()
        with tempfile.TemporaryDirectory() as temp:
            store = pathlib.Path(temp) / "printers.json"
            with mock.patch.object(self.plugin, "PRINTERS_FILE", str(store)):
                first = dashboard._add_manual({
                    "name": "Voron",
                    "url": "192.168.0.122",
                    "type": "moonraker",
                    "source": "orca",
                })
                second = dashboard._add_manual({
                    "name": "Voron renamed",
                    "url": "http://192.168.0.122/",
                    "type": "moonraker",
                    "source": "orca",
                })
                stored = self.plugin.load_manual_printers()

            self.assertEqual(first["id"], second["id"])
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["name"], "Voron renamed")

    def test_legacy_duplicate_picker_records_collapse_on_read(self):
        with tempfile.TemporaryDirectory() as temp:
            store = pathlib.Path(temp) / "printers.json"
            store.write_text(json.dumps([
                {"id": 1, "name": "First", "url": "http://printer"},
                {"id": 2, "name": "Latest", "url": "http://printer/"},
            ]), encoding="utf-8")
            with mock.patch.object(self.plugin, "PRINTERS_FILE", str(store)):
                stored = self.plugin.load_manual_printers()

            self.assertEqual(stored, [{
                "id": 1,
                "name": "Latest",
                "url": "http://printer",
            }])

    def test_picker_waits_for_python_acknowledgement(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn("function typeLabel(value){", source)
        self.assertNotIn("function typeLabel(t){", source)
        self.assertIn("pendingAdoptUrl = p.url || '';", source)
        self.assertIn("e.target.disabled = true;", source)
        self.assertIn("if (data.adoptResult)", source)
        self.assertIn("activeUrls[normalizedPrinterUrl(p.url)]", source)
        self.assertIn("var pendingFormAdd = false;", source)
        self.assertIn("orca.postMessage({ type:'adopt', discoveryRef:p.discovery_ref });", source)
        self.assertNotIn("orca.postMessage({ type:'adopt', printer:p });", source)

        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        entry = {"name": "Voron", "url": "http://printer", "type": "moonraker"}
        with mock.patch.object(dashboard, "_add_manual", return_value=dict(entry, id=1)), \
             mock.patch.object(dashboard, "_refresh_worker"):
            dashboard._add_and_refresh(entry, [])

        dashboard._post.assert_called_once_with({
            "adoptResult": {"url": "http://printer", "ok": True},
        })

    def test_saved_credentials_never_reach_the_dashboard_page(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        private = {
            "id": 1,
            "discovery_ref": "orca:fixture",
            "name": "Authenticated OctoPrint",
            "url": "http://octoprint.local",
            "type": "octoprint",
            "apikey": "octoprint-secret",
            "access_code": "bambu-secret",
        }

        self.assertEqual(
            self.plugin.public_printer_record(private),
            {
                "id": 1,
                "discovery_ref": "orca:fixture",
                "name": "Authenticated OctoPrint",
                "url": "http://octoprint.local",
                "type": "octoprint",
            },
        )
        with mock.patch.object(
            self.plugin,
            "load_manual_printers",
            return_value=[],
        ):
            self.assertEqual(
                self.plugin.available_orca_printers([private]),
                [self.plugin.public_printer_record(private)],
            )

        with mock.patch.object(
            self.plugin,
            "dashboard_printers",
            return_value=[dict(private)],
        ), mock.patch.object(
            self.plugin,
            "attach_outbox",
            return_value=0,
        ), mock.patch.object(
            dashboard,
            "_poll_and_push",
        ):
            dashboard._refresh_worker([dict(private)], False)

        payload = dashboard._post.call_args.args[0]
        serialized = json.dumps(payload)
        self.assertNotIn("octoprint-secret", serialized)
        self.assertNotIn("bambu-secret", serialized)
        self.assertEqual(payload["available"][0]["discovery_ref"], "orca:fixture")

        dashboard._post.reset_mock()
        with mock.patch.object(self.plugin, "autoname_printers"), mock.patch.object(
            self.plugin,
            "attach_outbox",
        ):
            dashboard._finish_poll_batch([dict(private)])
        serialized = json.dumps(dashboard._post.call_args.args[0])
        self.assertNotIn("octoprint-secret", serialized)
        self.assertNotIn("bambu-secret", serialized)

    def test_orca_picker_resolves_credentials_only_from_python_snapshot(self):
        dashboard = self.plugin.PrintersDashboard()
        private = {
            "discovery_ref": "orca:trusted",
            "name": "OctoPrint",
            "url": "http://octoprint.local",
            "type": "octoprint",
            "apikey": "trusted-secret",
        }
        dashboard._discovered_printers = {"orca:trusted": private}

        with mock.patch.object(
            dashboard,
            "_capture_discovered_printers",
            return_value=[private],
        ), mock.patch.object(self.plugin.ACTION_WORKER, "submit") as submit:
            dashboard.on_message({
                "type": "adopt",
                "discoveryRef": "orca:trusted",
                "printer": {
                    "url": "http://attacker.invalid",
                    "apikey": "attacker-secret",
                },
            })

            submit.assert_called_once()
            entry = submit.call_args.args[1]
            self.assertEqual(entry["url"], "http://octoprint.local")
            self.assertEqual(entry["apikey"], "trusted-secret")

            submit.reset_mock()
            dashboard.on_message({
                "type": "adopt",
                "discoveryRef": "orca:unknown",
                "printer": private,
            })
            submit.assert_not_called()

    def test_manual_octoprint_api_key_is_private_and_type_scoped(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn('id="f-apikey" type="password" autocomplete="off"', source)
        self.assertIn("document.getElementById('f-apikey').value = '';", source)
        dashboard = self.plugin.PrintersDashboard()

        with mock.patch.object(
            dashboard,
            "_capture_discovered_printers",
            return_value=[],
        ), mock.patch.object(self.plugin.ACTION_WORKER, "submit") as submit:
            dashboard.on_message({
                "type": "add",
                "name": "OctoPrint",
                "url": "octoprint.local",
                "kind": "octoprint",
                "apikey": "  secret-key  ",
                "access_code": "must-not-be-stored",
            })

            entry = submit.call_args.args[1]
            self.assertEqual(entry["apikey"], "secret-key")
            self.assertEqual(entry["access_code"], "")

            submit.reset_mock()
            dashboard.on_message({
                "type": "add",
                "name": "Moonraker",
                "url": "moonraker.local",
                "kind": "moonraker",
                "apikey": "must-not-be-stored",
            })
            self.assertEqual(submit.call_args.args[1]["apikey"], "")

    def test_plugin_does_not_forge_host_owned_process_manifest_refs(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertNotIn("OUTBOX_REF", source)
        self.assertNotIn("outbox-toggle", source)
        self.assertNotIn("set_outbox_binding", source)

    def test_curl_fallback_streams_without_a_temporary_file(self):
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stderr=b""))
        with mock.patch.object(self.plugin.shutil, "which", return_value="curl"), \
             mock.patch.object(self.plugin.subprocess, "run", run):
            self.assertEqual(self.plugin._curl_stor("printer", "code", "job.gcode", b"G28"), "")

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-T") + 1], "-")
        self.assertEqual(run.call_args.kwargs["input"], b"G28")

    def test_ftps_data_channel_reuses_the_control_tls_session(self):
        ftp = object.__new__(self.plugin._ImplicitFTPS)
        ftp._prot_p = True
        ftp.host = "printer"
        ftp.sock = types.SimpleNamespace(session="control-session")
        ftp.context = mock.Mock()
        ftp.context.wrap_socket.return_value = "tls-data"

        with mock.patch.object(
            self.plugin.ftplib.FTP,
            "ntransfercmd",
            return_value=("raw-data", 123),
        ):
            self.assertEqual(ftp.ntransfercmd("STOR job.gcode"), ("tls-data", 123))

        ftp.context.wrap_socket.assert_called_once_with(
            "raw-data",
            server_hostname="printer",
            session="control-session",
        )

    def test_host_push_tolerates_a_window_closing_mid_job(self):
        posted = []

        class Window:
            def is_open(self):
                return True

            def post(self, payload):
                posted.append(payload)

        self.assertTrue(self.plugin.post_window(Window(), {"status": "ok"}))
        self.assertEqual(posted, [{"status": "ok"}])
        self.assertFalse(self.plugin.post_window(None, {}))

    def test_reusable_pool_keeps_bursty_jobs_on_one_worker(self):
        pool = self.plugin.ReusableDaemonPool(
            "printers-test",
            max_workers=1,
            idle_timeout=0.2,
        )
        thread_ids = []
        done = threading.Event()

        def record():
            thread_ids.append(threading.get_ident())
            if len(thread_ids) == 2:
                done.set()

        pool.submit(record)
        pool.submit(record)

        self.assertTrue(done.wait(2))
        self.assertEqual(len(set(thread_ids)), 1)

    def test_pool_shutdown_discards_queued_printer_work(self):
        pool = self.plugin.ReusableDaemonPool(
            "printers-cancel-test",
            max_workers=1,
            idle_timeout=0.2,
        )
        started = threading.Event()
        release = threading.Event()
        ran = []

        def blocking_job():
            started.set()
            release.wait(2)

        first = pool.submit(blocking_job)
        self.assertTrue(started.wait(1))
        queued = pool.submit(lambda: ran.append("printer-command"))

        pool.shutdown(wait_timeout=0)
        pool.activate()
        current = pool.submit(lambda: ran.append("new-generation"))
        release.set()
        first.wait(1)
        current.wait(1)

        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            queued.wait(1)
        self.assertEqual(ran, ["new-generation"])

    def test_stale_worker_generation_cannot_post_to_window(self):
        plugin = _load_plugin()
        pool = plugin.ACTION_WORKER
        started = threading.Event()
        release = threading.Event()
        posted = []

        class Window:
            def is_open(self):
                return True

            def post(self, payload):
                posted.append(payload)

        def stale_post():
            started.set()
            release.wait(2)
            plugin.post_window(Window(), {"status": "stale"})

        job = pool.submit(stale_post)
        self.assertTrue(started.wait(1))
        pool.shutdown(wait_timeout=0)
        release.set()
        job.wait(1)

        self.assertEqual(posted, [])

    def test_dashboard_lifecycle_drops_queued_physical_command(self):
        plugin = _load_plugin(lifecycle=True)
        dashboard = plugin.PrintersDashboard()
        started = threading.Event()
        release = threading.Event()
        command = mock.Mock()

        with mock.patch.object(plugin, "configure_plugin_storage"), \
             mock.patch.object(dashboard, "_do_printer_cmd", command):
            dashboard.on_load()

            def blocking_job():
                started.set()
                release.wait(2)

            plugin.ACTION_WORKER.submit(blocking_job)
            self.assertTrue(started.wait(1))
            dashboard.on_message({
                "type": "printer-cmd",
                "url": "http://printer",
                "action": "cancel",
            })
            dashboard.on_cancelled()
            release.set()

        command.assert_not_called()

    def test_finished_upload_cannot_mutate_state_after_unload(self):
        plugin = _load_plugin(lifecycle=True)
        dashboard = plugin.PrintersDashboard()
        started = threading.Event()
        release = threading.Event()
        printer = {
            "name": "Printer",
            "url": "http://printer",
            "type": "moonraker",
        }
        record = {"file": "fixture.gcode", "name": "fixture.gcode"}

        def blocking_send(*args, **kwargs):
            started.set()
            release.wait(2)
            return {"ok": True}

        with tempfile.TemporaryDirectory() as temp:
            plugin.OUTBOX_DIR = temp
            pathlib.Path(temp, "fixture.gcode").write_bytes(b"G28")
            with mock.patch.object(plugin, "configure_plugin_storage"), \
                 mock.patch.object(plugin, "dashboard_printers", return_value=[printer]), \
                 mock.patch.object(plugin, "load_outbox_index", return_value=[record]), \
                 mock.patch.object(plugin, "poll_printer", return_value={
                     "online": True,
                     "state": "idle",
                 }), \
                 mock.patch.object(plugin, "send_gcode", side_effect=blocking_send), \
                 mock.patch.object(plugin, "record_print_started") as history, \
                 mock.patch.object(plugin, "outbox_remove") as remove:
                dashboard.on_load()
                job = plugin.ACTION_WORKER.submit(
                    dashboard._do_outbox_send,
                    {
                        "file": "fixture.gcode",
                        "url": "http://printer",
                        "start": True,
                    },
                )
                self.assertTrue(started.wait(1))
                dashboard.on_unload()
                release.set()
                job.wait(1)

        history.assert_not_called()
        remove.assert_not_called()

    def test_dashboard_uses_reusable_workers_instead_of_per_refresh_threads(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("threading.Thread("), 1)
        self.assertIn("POLL_WORKERS.submit", source)
        self.assertIn("ACTION_WORKER.submit", source)

    def test_network_discovery_runs_on_open_and_explicit_refresh(self):
        dashboard = self.plugin.PrintersDashboard()
        discovered = [{"name": "Configured", "url": "http://printer"}]

        with mock.patch.object(
            self.plugin,
            "discover_orca_printers",
            return_value=discovered,
        ), mock.patch.object(self.plugin.ACTION_WORKER, "submit") as submit:
            dashboard.on_message({"type": "ready"})
            submit.assert_called_once_with(
                dashboard._refresh_worker,
                discovered,
                True,
            )

            submit.reset_mock()
            dashboard.on_message({"type": "refresh"})
            submit.assert_called_once_with(
                dashboard._refresh_worker,
                discovered,
                True,
            )

    def test_empty_network_discovery_clears_stale_picker_entries(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)

        with mock.patch.object(self.plugin, "ssdp_bambu_names", return_value={}):
            dashboard._discover_and_push([])

        dashboard._post.assert_called_once_with({"discovered": []})

    def test_top_bar_keeps_manual_add_fields_compact_and_collapsed(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")

        self.assertIn('id="manual-form" class="manual-form" aria-hidden="true"', source)
        self.assertIn('id="manual-toggle" class="ghost"', source)
        self.assertIn("setManualFormOpen(true);", source)
        self.assertIn('class="compact-option"', source)
        self.assertIn('class="disclosure-icon" viewBox="0 0 12 8"', source)
        self.assertIn(
            '#manual-toggle[aria-expanded="true"] .disclosure-icon',
            source,
        )
        self.assertIn('<span aria-hidden="true">TLS</span>', source)
        self.assertNotIn('id="f-insecure-text"', source)
        self.assertLess(source.index('id="f-apikey"'), source.index('id="f-type"'))
        self.assertLess(source.index('id="f-type"'), source.index('id="add"'))

    def test_removing_a_printer_rechecks_lan_availability(self):
        dashboard = self.plugin.PrintersDashboard()
        discovered = [{"name": "Configured", "url": "http://printer"}]

        with mock.patch.object(dashboard, "_remove_manual") as remove, mock.patch.object(
            dashboard,
            "_refresh_worker",
        ) as refresh:
            dashboard._remove_and_refresh(7, discovered)

        remove.assert_called_once_with(7)
        refresh.assert_called_once_with(discovered, discover_network=True)

    def test_each_printer_status_is_pushed_without_waiting_for_slower_peers(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = {"url": "http://printer"}
        status = {"online": True, "state": "ready"}

        with mock.patch.object(self.plugin, "poll_printer", return_value=status):
            dashboard._poll_into(printer)

        self.assertEqual(printer["status"], status)
        dashboard._post.assert_called_once_with({
            "printerStatus": {
                "url": "http://printer",
                "status": status,
            },
        })

    def test_stale_poll_cannot_restore_a_removed_printer(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._list_generation = 2
        dashboard._post = mock.Mock(return_value=True)
        completed = []
        stale_printers = [{"id": 4, "url": "http://removed"}]

        with mock.patch.object(self.plugin, "autoname_printers") as autoname, \
             mock.patch.object(self.plugin, "attach_outbox") as attach:
            dashboard._finish_poll_batch(
                stale_printers,
                on_complete=lambda: completed.append(True),
                generation=1,
            )

        autoname.assert_not_called()
        attach.assert_not_called()
        dashboard._post.assert_not_called()
        self.assertEqual(completed, [True])

    def test_stale_status_result_is_not_cached_or_pushed(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._list_generation = 2
        dashboard._post = mock.Mock(return_value=True)
        printer = {"url": "http://removed"}

        with mock.patch.object(
            self.plugin,
            "poll_printer",
            return_value={"online": True, "state": "ready"},
        ):
            dashboard._poll_into(printer, generation=1)

        self.assertNotIn("http://removed", dashboard._status_cache)
        dashboard._post.assert_not_called()

    def test_dashboard_shows_unknown_status_as_checking(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn("data.printerStatus", source)
        self.assertIn("s.online === true", source)
        self.assertIn("'checking'", source)

    def test_only_end_to_end_backends_are_active(self):
        self.assertEqual(self.plugin.backend_for_type("moonraker"), "moonraker")
        self.assertEqual(self.plugin.backend_for_type("octoprint"), "octoprint")
        self.assertEqual(self.plugin.backend_for_type("bambu"), "bambu")
        self.assertEqual(self.plugin.backend_for_type("prusalink"), "unavailable")
        self.assertEqual(self.plugin.backend_for_type("repetier"), "unavailable")
        self.assertFalse(self.plugin.backend_is_active("duet"))

        status = self.plugin.poll_printer({
            "url": "http://printer",
            "type": "duet",
        })
        self.assertIsNone(status["online"])
        self.assertFalse(status["supported"])
        self.assertEqual(status["state"], "not available yet")

    def test_planned_backends_are_visible_but_disabled(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn(
            '<option value="prusalink" disabled>PrusaLink — coming later</option>',
            source,
        )
        self.assertIn(
            '<option value="duet" disabled>Duet — coming later</option>',
            source,
        )
        self.assertEqual(
            self.plugin.UI_COPY["en"]["integrationUnavailable"],
            "Printer integration is not available yet",
        )

    def test_refresh_preserves_known_status_while_a_new_printer_checks(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        known = {"online": True, "state": "ready"}
        dashboard._status_cache["http://known"] = known
        printers = [
            {"url": "http://known", "name": "Known"},
            {"url": "http://new", "name": "New"},
        ]

        with mock.patch.object(
            self.plugin,
            "dashboard_printers",
            return_value=printers,
        ), mock.patch.object(
            self.plugin,
            "attach_outbox",
            return_value=0,
        ), mock.patch.object(
            dashboard,
            "_poll_and_push",
        ):
            dashboard._refresh_worker([], False)

        payload = dashboard._post.call_args.args[0]
        self.assertEqual(payload["printers"][0]["status"], known)
        self.assertEqual(payload["printers"][1]["status"]["state"], "checking")

    def test_poll_batch_does_not_block_the_action_worker(self):
        source = inspect.getsource(self.plugin.PrintersDashboard._poll_and_push)
        self.assertNotIn(".wait(", source)
        completed = []
        batch = self.plugin.PollBatch(2, lambda: completed.append(True))
        batch.finish_one()
        self.assertEqual(completed, [])
        batch.finish_one()
        self.assertEqual(completed, [True])

    def test_busy_main_print_action_is_disabled_and_controls_keep_their_slot(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn("var printAttrs = selectable", source)
        self.assertIn("t('printerBusy')", source)
        self.assertIn('class="tile-actions"><div class="ctl-row">', source)
        self.assertIn('class="ctl-slot"', source)
        self.assertIn("class=\"ctl spacer\"", source)

    def test_orca_language_drives_page_and_native_messages(self):
        with mock.patch.object(
            self.plugin.orca.host,
            "app_language",
            return_value="ru_RU",
            create=True,
        ):
            rendered = self.plugin.render_page()

        self.assertIn("var hostLanguage = 'ru';", rendered)
        self.assertIn("Отправить G-code на выбранные", rendered)
        self.assertEqual(
            self.plugin.ui_text("dashboardOpened"),
            "Панель Print Farm открыта.",
        )

    def test_all_orca_locales_are_preserved_with_per_key_english_fallback(self):
        for locale in self.plugin.ORCA_UI_LOCALES:
            self.assertEqual(self.plugin.normalize_ui_language(locale), locale)

        original = self.plugin.UI_COPY
        try:
            self.plugin.UI_COPY = {
                "en": {"shared": "English", "englishOnly": "Fallback"},
                "de": {"shared": "Deutsch"},
            }
            self.assertEqual(
                self.plugin.resolved_ui_catalog("de_DE"),
                {"shared": "Deutsch", "englishOnly": "Fallback"},
            )
            self.assertEqual(
                self.plugin.resolved_ui_catalog("pt_BR"),
                {"shared": "English", "englishOnly": "Fallback"},
            )
        finally:
            self.plugin.UI_COPY = original

    def test_bundled_locale_catalogs_are_valid(self):
        spec = importlib.util.spec_from_file_location(
            "printers_locale_validator_test", LOCALE_VALIDATOR_PATH
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(validator)

        self.assertEqual(validator.validate_catalogs(), [])

    def test_every_orca_locale_has_a_complete_catalog(self):
        locale_dir = PLUGIN_PATH.with_name("printers_locales")
        english_keys = set(json.loads(
            (locale_dir / "en.json").read_text(encoding="utf-8")
        ))

        self.assertEqual(
            {path.stem for path in locale_dir.glob("*.json")},
            set(self.plugin.ORCA_UI_LOCALES),
        )
        for locale in self.plugin.ORCA_UI_LOCALES:
            with self.subTest(locale=locale):
                catalog = json.loads(
                    (locale_dir / (locale + ".json")).read_text(encoding="utf-8")
                )
                self.assertEqual(set(catalog), english_keys)

    def test_mass_print_rechecks_busy_state_before_send(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        printer = {
            "name": "Busy",
            "url": "http://printer",
            "type": "moonraker",
        }
        message = {
            "dataB64": "RzI4",
            "filename": "job.gcode",
            "urls": ["http://printer"],
            "start": True,
        }

        with mock.patch.object(
            self.plugin,
            "dashboard_printers",
            return_value=[printer],
        ), mock.patch.object(
            self.plugin,
            "poll_printer",
            return_value={"online": True, "state": "printing"},
        ), mock.patch.object(
            self.plugin,
            "send_gcode",
        ) as send, mock.patch.object(
            dashboard,
            "_refresh_worker",
        ):
            dashboard._do_mass_print(message)

        send.assert_not_called()
        status_payload = dashboard._post.call_args.args[0]
        self.assertIn("printer is busy", status_payload["status"])


class OctoPrintTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def test_status_uses_authenticated_printer_and_job_endpoints(self):
        printer_response = {
            "temperature": {
                "tool0": {"actual": 214.5, "target": 215.0},
                "bed": {"actual": 59.5, "target": 60.0},
            },
            "state": {"text": "Printing"},
        }
        job_response = {
            "progress": {"completion": 42.4, "printTime": 120},
            "job": {"file": {"name": "fixture.gcode", "path": "fixture.gcode"}},
        }
        context = mock.sentinel.ssl_context

        with mock.patch.object(
            self.plugin,
            "_get_json",
            side_effect=[printer_response, job_response],
        ) as get_json, mock.patch.object(
            self.plugin,
            "octoprint_thumbnail",
            return_value="data:image/png;base64,fixture",
        ) as thumbnail:
            status = self.plugin.poll_octoprint(
                "http://octoprint.local",
                context,
                "secret-key",
            )

        headers = {"Accept": "application/json", "X-Api-Key": "secret-key"}
        self.assertEqual(
            get_json.call_args_list,
            [
                mock.call(
                    "http://octoprint.local/api/printer",
                    context,
                    headers=headers,
                ),
                mock.call(
                    "http://octoprint.local/api/job",
                    context,
                    headers=headers,
                ),
            ],
        )
        thumbnail.assert_called_once_with(
            "http://octoprint.local",
            headers,
            context,
            "fixture.gcode",
        )
        self.assertEqual(status["state"], "Printing")
        self.assertEqual(status["progress"], 42)
        self.assertEqual(status["filename"], "fixture.gcode")
        self.assertEqual(status["nozzle"], 214.5)
        self.assertEqual(status["bed"], 59.5)

    def test_disconnected_printer_keeps_reachable_octoprint_online(self):
        disconnected = urllib.error.HTTPError(
            "http://octoprint.local/api/printer",
            409,
            "Conflict",
            {},
            None,
        )
        context = mock.sentinel.ssl_context
        with mock.patch.object(
            self.plugin,
            "_get_json",
            side_effect=[
                disconnected,
                {"current": {"state": "Closed"}},
                {"progress": {}, "job": {"file": {}}},
            ],
        ) as get_json:
            status = self.plugin.poll_octoprint(
                "http://octoprint.local",
                context,
                "secret-key",
            )

        self.assertTrue(status["online"])
        self.assertEqual(status["state"], "Closed")
        self.assertIsNone(status["nozzle"])
        self.assertEqual(
            [call.args[0] for call in get_json.call_args_list],
            [
                "http://octoprint.local/api/printer",
                "http://octoprint.local/api/connection",
                "http://octoprint.local/api/job",
            ],
        )

    def test_authentication_failure_does_not_fall_back_to_false_online(self):
        unauthorized = urllib.error.HTTPError(
            "http://octoprint.local/api/printer",
            401,
            "Unauthorized",
            {},
            None,
        )
        with mock.patch.object(
            self.plugin,
            "_get_json",
            side_effect=unauthorized,
        ) as get_json:
            with self.assertRaises(urllib.error.HTTPError):
                self.plugin.poll_octoprint(
                    "http://octoprint.local",
                    mock.sentinel.ssl_context,
                    "wrong-key",
                )

        get_json.assert_called_once()

    def test_controls_use_authenticated_job_endpoint(self):
        printer = {
            "url": "http://octoprint.local/",
            "type": "octoprint",
            "apikey": "secret-key",
        }
        expected_payloads = {
            "pause": {"command": "pause", "action": "pause"},
            "resume": {"command": "pause", "action": "resume"},
            "cancel": {"command": "cancel"},
        }

        for action, expected_payload in expected_payloads.items():
            with self.subTest(action=action), mock.patch.object(
                self.plugin,
                "ssl_ctx_for",
                return_value=mock.sentinel.ssl_context,
            ), mock.patch.object(self.plugin, "_post_json") as post_json:
                result = self.plugin.printer_command(printer, action)

            self.assertEqual(result, {"ok": True})
            post_json.assert_called_once_with(
                "http://octoprint.local/api/job",
                mock.sentinel.ssl_context,
                {"X-Api-Key": "secret-key"},
                expected_payload,
                timeout=self.plugin.CONTROL_TIMEOUT,
            )

    def test_upload_uses_authenticated_local_files_endpoint(self):
        printer = {
            "url": "http://octoprint.local",
            "type": "octoprint",
            "apikey": "secret-key",
        }

        with mock.patch.object(
            self.plugin,
            "ssl_ctx_for",
            return_value=mock.sentinel.ssl_context,
        ), mock.patch.object(self.plugin, "_multipart_upload") as upload:
            result = self.plugin.send_gcode(
                printer,
                "fixture.gcode",
                b"G28\n",
                start=True,
            )

        self.assertEqual(result, {"ok": True})
        upload.assert_called_once_with(
            "http://octoprint.local/api/files/local",
            mock.sentinel.ssl_context,
            {"X-Api-Key": "secret-key"},
            "fixture.gcode",
            b"G28\n",
            {"select": "true", "print": "true"},
        )

    def test_transport_errors_are_reported_without_false_success(self):
        printer = {
            "url": "http://octoprint.local",
            "type": "octoprint",
            "apikey": "secret-key",
        }
        with mock.patch.object(
            self.plugin,
            "_multipart_upload",
            side_effect=RuntimeError("401 Unauthorized"),
        ):
            result = self.plugin.send_gcode(
                printer,
                "fixture.gcode",
                b"G28\n",
                start=False,
            )

        self.assertFalse(result["ok"])
        self.assertIn("401 Unauthorized", result["error"])


class HostApiCompatibilityTest(unittest.TestCase):
    def test_pages_capability_is_primary_when_host_exposes_it(self):
        registered = []
        plugin = _load_plugin(
            lifecycle=True,
            pages=True,
            registered=registered,
        )

        plugin.PrintersPlugin().register_capabilities()

        self.assertEqual(registered, [plugin.PrintersDashboard, plugin.PrintersOutbox])
        self.assertTrue(issubclass(
            plugin.PrintersDashboard,
            plugin.orca.pages.PagesPluginCapabilityBase,
        ))
        self.assertFalse(plugin._PLUGIN_RUNTIME_ACTIVE)
        dashboard = plugin.PrintersDashboard()
        self.assertIn("orca.postMessage({ type:'ready' })", dashboard.get_ui())
        self.assertIn("if (document.hidden) return;", dashboard.get_ui())
        with mock.patch.object(dashboard, "post_message") as post:
            self.assertTrue(dashboard._post({"status": "ready"}))
        post.assert_called_once_with({"status": "ready"})

    def test_script_window_remains_the_old_host_fallback(self):
        registered = []
        plugin = _load_plugin(lifecycle=True, registered=registered)

        plugin.PrintersPlugin().register_capabilities()

        self.assertEqual(registered, [plugin.PrintersDashboard, plugin.PrintersOutbox])
        self.assertIsNone(plugin._PAGE_CAPABILITY_BASE)
        self.assertTrue(issubclass(
            plugin.PrintersDashboard,
            plugin.orca.script.ScriptPluginCapabilityBase,
        ))

    def test_printer_viewer_accepts_lan_http_urls_and_confines_navigation(self):
        plugin = _load_plugin()

        page = plugin.render_viewer_page(
            "https://printer.local:8443/ui/?token=a&mode=full#status"
        )

        self.assertIn("frame-src https://printer.local:8443", page)
        self.assertIn(
            'src="https://printer.local:8443/ui/?token=a&amp;mode=full#status"',
            page,
        )
        self.assertIn("allow-same-origin allow-scripts allow-forms", page)
        self.assertNotIn("allow-popups", page)
        self.assertNotIn("allow-top-navigation", page)
        self.assertEqual(
            plugin.validated_printer_web_url("printer:7125"),
            "http://printer:7125",
        )
        self.assertEqual(
            plugin.validated_printer_web_url("[::1]:8080"),
            "http://[::1]:8080",
        )

    def test_printer_viewer_rejects_markup_credentials_and_non_http_urls(self):
        plugin = _load_plugin()
        rejected = (
            'http://printer.local/\"><script>alert(1)</script>',
            "http://user:password@printer.local/",
            "javascript:alert(1)",
            "http://printer.local/\nnext",
            "http://printer.local\\@example.com/",
            "http://printer local/",
            "file:///etc/passwd",
        )

        for value in rejected:
            with self.subTest(value=value):
                self.assertEqual(plugin.validated_printer_web_url(value), "")
                self.assertEqual(plugin.render_viewer_page(value), "")

    def test_invalid_viewer_url_never_creates_a_host_window(self):
        plugin = _load_plugin()
        dashboard = plugin.PrintersDashboard()
        create_window = mock.Mock()

        with mock.patch.object(
            plugin.orca.host.ui,
            "create_window",
            create_window,
            create=True,
        ):
            self.assertFalse(dashboard._open_viewer("https://user:pw@printer", "Printer"))

        create_window.assert_not_called()

    def test_dashboard_unload_closes_owned_printer_windows(self):
        plugin = _load_plugin(lifecycle=True)
        dashboard = plugin.PrintersDashboard()
        viewer = mock.Mock()
        viewer.is_open.return_value = True

        with mock.patch.object(plugin, "configure_plugin_storage"), \
             mock.patch.object(
                 plugin.orca.host.ui,
                 "create_window",
                 return_value=viewer,
                 create=True,
             ):
            dashboard.on_load()
            self.assertTrue(dashboard._open_viewer("http://printer.local", "Printer"))
            dashboard.on_unload()

        viewer.close.assert_called_once_with()
        self.assertEqual(dashboard._viewers, [])


class PrintHistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.plugin.PRINT_HISTORY_FILE = str(
            pathlib.Path(self.temp_dir.name) / "print_history.json"
        )
        self.printer = {
            "id": 7,
            "name": "Voron",
            "url": "http://voron.local",
            "type": "moonraker",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_observed_job_keeps_one_record_across_pause_and_completion(self):
        self.assertTrue(self.plugin.record_print_observation(
            self.printer,
            {
                "state": "printing",
                "filename": "parts/cube.gcode",
                "progress": 25,
                "elapsed_seconds": 30,
            },
            now=1_000,
        ))
        self.assertTrue(self.plugin.record_print_observation(
            self.printer,
            {"state": "paused", "filename": "cube.gcode", "progress": 50},
            now=1_060,
        ))
        self.assertTrue(self.plugin.record_print_observation(
            self.printer,
            {
                "state": "complete",
                "filename": "cube.gcode",
                "progress": 100,
                "elapsed_seconds": 120,
            },
            now=1_120,
        ))

        history = self.plugin.print_history_for_printer(self.printer)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["filename"], "cube.gcode")
        self.assertEqual(history[0]["state"], "completed")
        self.assertEqual(history[0]["progress"], 100)
        self.assertEqual(history[0]["duration_seconds"], 150)
        self.assertEqual(history[0]["source"], "observed")

    def test_partial_job_returning_to_idle_is_ended_not_claimed_complete(self):
        self.plugin.record_print_observation(
            self.printer,
            {"state": "printing", "filename": "fixture.gcode", "progress": 30},
            now=2_000,
        )
        self.plugin.record_print_observation(
            self.printer,
            {"state": "standby", "filename": "fixture.gcode", "progress": 30},
            now=2_100,
        )

        self.assertEqual(
            self.plugin.print_history_for_printer(self.printer)[0]["state"],
            "ended",
        )

    def test_repeated_terminal_snapshot_does_not_duplicate_history(self):
        terminal = {
            "state": "finished",
            "filename": "fixture.gcode",
            "progress": 100,
            "elapsed_seconds": 90,
        }
        self.plugin.record_print_observation(self.printer, terminal, now=3_000)
        self.plugin.record_print_observation(self.printer, terminal, now=3_015)

        self.assertEqual(len(self.plugin.print_history_for_printer(self.printer)), 1)

    def test_print_farm_start_is_persisted_before_the_first_poll(self):
        record = self.plugin.record_print_started(
            self.printer,
            "fresh.gcode",
            now=4_000,
        )

        self.assertEqual(record["source"], "print_farm")
        self.assertEqual(record["state"], "printing")
        self.assertEqual(
            self.plugin.print_history_for_printer(self.printer)[0]["filename"],
            "fresh.gcode",
        )

    def test_accepted_start_ignores_an_immediate_stale_idle_poll(self):
        self.plugin.record_print_started(self.printer, "fresh.gcode", now=4_000)

        self.assertFalse(self.plugin.record_print_observation(
            self.printer,
            {"state": "idle", "filename": "", "progress": 0},
            now=4_010,
        ))

        record = self.plugin.print_history_for_printer(self.printer)[0]
        self.assertEqual(record["state"], "printing")
        self.assertIsNone(record["finished_at"])

    def test_malformed_numeric_fields_are_sanitized_when_history_is_loaded(self):
        pathlib.Path(self.plugin.PRINT_HISTORY_FILE).write_text(
            json.dumps([{
                "id": "broken",
                "printer_key": "url:http://voron.local",
                "state": "printing",
                "started_at": "not-a-time",
                "updated_at": {},
                "duration_seconds": "forever",
                "progress": "lots",
            }]),
            encoding="utf-8",
        )

        record = self.plugin.load_print_history()[0]

        self.assertEqual(record["started_at"], 0)
        self.assertEqual(record["updated_at"], 0)
        self.assertEqual(record["duration_seconds"], 0)
        self.assertIsNone(record["progress"])

    def test_reused_numeric_id_does_not_inherit_another_printer_history(self):
        self.plugin.record_print_started(self.printer, "voron.gcode", now=4_000)
        replacement = dict(self.printer, url="http://replacement.local")

        self.assertEqual(self.plugin.print_history_for_printer(replacement), [])

    def test_successful_mass_print_records_the_accepted_start(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        message = {
            "dataB64": "RzI4",
            "filename": "fresh.gcode",
            "urls": [self.printer["url"]],
            "start": True,
        }

        with mock.patch.object(
            self.plugin,
            "dashboard_printers",
            return_value=[self.printer],
        ), mock.patch.object(
            self.plugin,
            "poll_printer",
            return_value={"online": True, "state": "idle"},
        ), mock.patch.object(
            self.plugin,
            "send_gcode",
            return_value={"ok": True},
        ), mock.patch.object(
            self.plugin,
            "record_print_started",
        ) as record_started, mock.patch.object(
            dashboard,
            "_refresh_worker",
        ):
            dashboard._do_mass_print(message)

        record_started.assert_called_once_with(self.printer, "fresh.gcode")

    def test_dashboard_pushes_only_the_requested_printer_history(self):
        dashboard = self.plugin.PrintersDashboard()
        dashboard._post = mock.Mock(return_value=True)
        other = {
            "id": 8,
            "name": "Other",
            "url": "http://other.local",
            "type": "octoprint",
        }
        self.plugin.record_print_started(self.printer, "voron.gcode", now=5_000)
        self.plugin.record_print_started(other, "other.gcode", now=5_001)

        with mock.patch.object(
            self.plugin,
            "dashboard_printers",
            return_value=[self.printer, other],
        ):
            dashboard._post_print_history({
                "id": self.printer["id"],
                "url": self.printer["url"],
            })

        payload = dashboard._post.call_args.args[0]["printHistory"]
        self.assertEqual(payload["printerId"], 7)
        self.assertEqual([item["filename"] for item in payload["items"]], ["voron.gcode"])

    def test_history_button_is_per_printer_and_explains_observation_scope(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertIn('class="history ghost"', source)
        self.assertIn("openPrintHistory(p)", source)
        self.assertIn("text('history-note', 'historyCoverage')", source)


if __name__ == "__main__":
    unittest.main()
