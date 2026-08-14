import importlib.util
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


def _load_plugin():
    fake_orca = types.ModuleType("orca")
    fake_orca.base = object
    fake_orca.plugin = lambda cls: cls
    fake_orca.register_capability = lambda capability: None
    fake_orca.ExecutionResult = _ExecutionResult
    fake_orca.PluginResult = types.SimpleNamespace(RecoverableError="recoverable")
    fake_orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=object)
    fake_orca.host = types.SimpleNamespace(ui=types.SimpleNamespace())
    fake_orca.slicing = types.SimpleNamespace(
        SlicingPipelineCapabilityBase=object,
        Step=types.SimpleNamespace(psGCodePostProcess="post-process"),
    )
    sys.modules["orca"] = fake_orca

    spec = importlib.util.spec_from_file_location("printers_plugin_under_test", PLUGIN_PATH)
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

            self.assertEqual(package.name, "printers-0.0.4")
            self.assertEqual(
                (package / "printers_plugin.py").read_bytes(),
                PLUGIN_PATH.read_bytes(),
            )
            self.assertEqual(
                (package / "printers.svg").read_bytes(),
                PLUGIN_PATH.with_name("printers.svg").read_bytes(),
            )
            self.assertEqual(
                {path.name for path in (package / "printers_locales").glob("*.json")},
                {"en.json", "ru.json", "zh_CN.json", "zh_TW.json"},
            )
            metadata = json.loads(
                (package / "package-metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["locales"], ["en", "ru", "zh_CN", "zh_TW"])

    def test_release_notes_use_only_current_changelog_section(self):
        spec = importlib.util.spec_from_file_location("printers_build_under_test", BUILD_PATH)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)

        notes = builder.render_release_notes("0.0.4")

        self.assertIn("## Print Farm 0.0.4", notes)
        self.assertIn("Made add, remove, refresh", notes)
        self.assertNotIn("Bambu printers are now discovered", notes)


class PluginStorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def test_host_storage_migrates_state_without_deleting_legacy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            legacy = root / "plugin"
            storage = root / "plugin_data" / "printers"
            legacy.mkdir()
            (legacy / "printers.json").write_text('[{"id":1}]', encoding="utf-8")
            (legacy / "outbox_index.json").write_text(
                '[{"file":"fixture.gcode"}]', encoding="utf-8"
            )
            (legacy / "outbox").mkdir()
            (legacy / "outbox" / "fixture.gcode").write_text("G28", encoding="utf-8")

            plugin_host = types.SimpleNamespace(storage=lambda: str(storage))
            with mock.patch.object(self.plugin, "PLUGIN_DIR", str(legacy)), \
                 mock.patch.object(self.plugin, "PLUGIN_STORAGE_DIR", str(legacy)), \
                 mock.patch.object(self.plugin, "PRINTERS_FILE", str(legacy / "printers.json")), \
                 mock.patch.object(self.plugin, "OUTBOX_DIR", str(legacy / "outbox")), \
                 mock.patch.object(self.plugin, "OUTBOX_INDEX", str(legacy / "outbox_index.json")), \
                 mock.patch.object(self.plugin.orca.host, "plugin", plugin_host, create=True):
                self.plugin.PrintersPlugin().register_capabilities()

                self.assertEqual(self.plugin.PLUGIN_STORAGE_DIR, str(storage))
                self.assertEqual(self.plugin.PRINTERS_FILE, str(storage / "printers.json"))
                self.assertEqual(self.plugin.OUTBOX_DIR, str(storage / "outbox"))
                self.assertEqual(
                    (storage / "printers.json").read_text(encoding="utf-8"),
                    '[{"id":1}]',
                )
                self.assertEqual(
                    (storage / "outbox" / "fixture.gcode").read_text(encoding="utf-8"),
                    "G28",
                )

            self.assertTrue((legacy / "printers.json").exists())
            self.assertTrue((legacy / "outbox" / "fixture.gcode").exists())

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

    def test_dashboard_uses_reusable_workers_instead_of_per_refresh_threads(self):
        source = PLUGIN_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count("threading.Thread("), 1)
        self.assertIn("POLL_WORKERS.submit", source)
        self.assertIn("ACTION_WORKER.submit", source)

    def test_network_discovery_runs_only_after_explicit_refresh(self):
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
                False,
            )

            submit.reset_mock()
            dashboard.on_message({"type": "refresh"})
            submit.assert_called_once_with(
                dashboard._refresh_worker,
                discovered,
                True,
            )

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


if __name__ == "__main__":
    unittest.main()
