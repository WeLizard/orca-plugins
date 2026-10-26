import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import types
import unittest
import unittest.mock

import plugin_settings as ps
from test_printers_plugin import _Enum, _fake_lifecycle, _load_plugin

ROOT = pathlib.Path(__file__).resolve().parent
OUT, DASH = ps.OUTBOX, ps.DASHBOARD


class SettingsValidationTest(unittest.TestCase):
    def test_defaults_are_explicit_and_match_current_behaviour(self):
        self.assertEqual(ps.defaults(OUT), {
            "keep_copies": True, "journal_orca_events": True, "profile_hint": True,
            "auto_delete": False, "auto_delete_days": 90, "keep_last": 0})
        self.assertEqual(ps.defaults(DASH), {
            "keep_history": True, "poll_seconds": 15, "pause_hidden": True})
        for kind in (OUT, DASH):
            clean = {k: v for k, v in ps.normalize(kind, None).items()
                     if k != "clear_history_token"}
            self.assertEqual(clean, ps.defaults(kind))

    def test_junk_and_wrong_types_fall_back_to_defaults(self):
        for raw in (None, "", "{", "[]", "null", "12", b"\xff", object(), [], 5, "{\"a\":"):
            self.assertEqual(ps.normalize(OUT, raw), ps.defaults(OUT))
        bad = {"keep_copies": "no", "journal_orca_events": 0, "profile_hint": None,
               "auto_delete": 1, "auto_delete_days": True}
        self.assertEqual(ps.normalize(OUT, bad), ps.defaults(OUT))
        bad = {"keep_history": "x", "poll_seconds": 7, "pause_hidden": 1}
        self.assertEqual(ps.normalize(DASH, bad)["poll_seconds"], 15)
        self.assertEqual({k: v for k, v in ps.normalize(DASH, bad).items()
                          if k != "clear_history_token"}, ps.defaults(DASH))
        for value in (True, 15.0, "15", 0, -5, 10 ** 30):
            self.assertEqual(ps.normalize(DASH, {"poll_seconds": value})["poll_seconds"], 15)
        self.assertEqual(ps.normalize(OUT, {"auto_delete_days": 45})["auto_delete_days"], 90)

    def test_valid_values_and_json_text_are_accepted_unknown_keys_ignored(self):
        text = json.dumps({"keep_copies": False, "auto_delete": True, "auto_delete_days": 30,
                           "from_the_future": 1, "api_key": "nope"})
        result = ps.normalize(OUT, text)
        self.assertEqual((result["keep_copies"], result["auto_delete"],
                          result["auto_delete_days"]), (False, True, 30))
        self.assertNotIn("from_the_future", result)
        self.assertNotIn("api_key", result)
        for seconds in ps.POLL_CHOICES:
            self.assertEqual(ps.normalize(DASH, {"poll_seconds": seconds})["poll_seconds"], seconds)

    def test_clear_token_is_a_short_plain_string_only(self):
        self.assertEqual(ps.normalize(DASH, {"clear_history_token": "ab-12_x.y:z"})["clear_history_token"],
                         "ab-12_x.y:z")
        for token in (5, "", "a b", "x" * 81, "<script>", None):
            self.assertEqual(ps.normalize(DASH, {"clear_history_token": token})["clear_history_token"], "")
        self.assertNotIn("clear_history_token", ps.normalize(OUT, {"clear_history_token": "abc"}))


class _Owner:
    def __init__(self, config):
        self.config = config
        self.calls = 0

    def get_config(self):
        self.calls += 1
        if isinstance(self.config, Exception):
            raise self.config
        return self.config if isinstance(self.config, str) else json.dumps(self.config)


class RuntimeSettingsTest(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()
        self.events = _fake_lifecycle(self.plugin)
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        p = self.plugin
        p.PLUGIN_STORAGE_DIR = str(self.root)
        p.PRINT_HISTORY_FILE = str(self.root / "print_history.json")
        p.PRINTERS_FILE = str(self.root / "printers.json")
        p.OUTBOX_DIR = str(self.root / "outbox")
        p.OUTBOX_INDEX = str(self.root / "outbox_index.json")
        p._STORAGE_DENIED = False
        p._HISTORY_STORE_DAMAGED = False
        p.ORCA_HINT_DELAY_SECONDS = 0.01
        self.addCleanup(self.temp.cleanup)

    def set_config(self, kind, config):
        owner = _Owner(config)
        self.plugin._SETTINGS_OWNERS[kind] = owner
        self.plugin._SETTINGS_CACHE.clear()
        return owner

    def test_missing_owner_error_or_junk_gives_defaults(self):
        p = self.plugin
        self.assertEqual(p.plugin_settings(OUT), ps.defaults(OUT))
        self.set_config(OUT, RuntimeError("no host"))
        self.assertEqual(p.plugin_settings(OUT), ps.defaults(OUT))
        self.set_config(OUT, "not json")
        self.assertEqual(p.plugin_settings(OUT), ps.defaults(OUT))

    def test_reads_are_cached_and_expire(self):
        p = self.plugin
        owner = self.set_config(DASH, {"poll_seconds": 30})
        for _ in range(20):
            self.assertEqual(p.plugin_settings(DASH)["poll_seconds"], 30)
        self.assertEqual(owner.calls, 1)
        owner.config = {"poll_seconds": 5}
        self.assertEqual(p.plugin_settings(DASH)["poll_seconds"], 30)
        p._SETTINGS_CACHE[DASH] = (time.monotonic() - p._SETTINGS_TTL - 1, p._SETTINGS_CACHE[DASH][1])
        self.assertEqual(p.plugin_settings(DASH)["poll_seconds"], 5)
        owner.config = {"poll_seconds": 60}
        self.assertEqual(p.plugin_settings(DASH, fresh=True)["poll_seconds"], 60)

    def test_capabilities_register_as_owners_and_expose_defaults(self):
        p = self.plugin
        outbox, dashboard = p.PrintersOutbox(), p.PrintersDashboard()
        self.assertIs(p._SETTINGS_OWNERS[OUT], outbox)
        self.assertIs(p._SETTINGS_OWNERS[DASH], dashboard)
        for cap, kind in ((outbox, OUT), (dashboard, DASH)):
            self.assertTrue(cap.has_config_ui())
            self.assertEqual(cap.get_default_config(), ps.defaults(kind))

    # --- Printers Outbox ---------------------------------------------------
    def execute(self, outbox):
        gcode = self.root / "fresh.gcode"
        gcode.write_text("; printer_settings_id = Voron\nG28\n", encoding="utf-8")
        ctx = types.SimpleNamespace(step="post-process", gcode_path=str(gcode),
                                    output_name="fresh.gcode", host="")
        return outbox.execute(ctx)

    def test_copies_off_skips_without_copying_and_default_copies(self):
        p = self.plugin
        outbox = p.PrintersOutbox()
        self.set_config(OUT, {"keep_copies": False})
        result = self.execute(outbox)
        self.assertEqual(result[0], "skipped")
        self.assertEqual(result[1], p.ui_text("outboxCopiesOff"))
        self.assertFalse((self.root / "outbox").exists())
        self.assertEqual(p.load_outbox_index(), [])
        self.set_config(OUT, {})
        self.assertEqual(self.execute(outbox)[0], "success")
        self.assertEqual(len(p.load_outbox_index()), 1)

    def fire(self, outbox, event, **fields):
        fields.setdefault("code", _Enum("Ok"))
        outbox.on_lifecycle_event(getattr(self.events, event), types.SimpleNamespace(**fields))

    def test_journal_setting_controls_orca_events_only(self):
        p = self.plugin
        outbox = p.PrintersOutbox()
        self.set_config(OUT, {"journal_orca_events": False})
        self.fire(outbox, "SendJobFinished", name="a", device_id="", source="send_job")
        self.assertEqual(p.load_print_history(), [])
        self.set_config(OUT, {"journal_orca_events": True})
        self.fire(outbox, "SendJobFinished", name="b", device_id="", source="send_job")
        self.assertEqual([r["filename"] for r in p.load_print_history()], ["b"])

    def test_profile_hint_setting_controls_the_flag(self):
        p = self.plugin
        with p._ORCA_EVENT_LOCK:
            p._ORCA_STATE["no_hook"] = True
        self.set_config(OUT, {})
        self.assertTrue(p.outbox_hint_visible())
        self.set_config(OUT, {"profile_hint": False})
        self.assertFalse(p.outbox_hint_visible())
        self.assertTrue(p.outbox_hook_missing())

    def add_record(self, name, age_days, file_exists=True):
        p = self.plugin
        folder = pathlib.Path(p.OUTBOX_DIR) / "unassigned"
        folder.mkdir(parents=True, exist_ok=True)
        stored = "unassigned/" + name
        if file_exists:
            (folder / name).write_text("G28", encoding="utf-8")
        index = p.load_outbox_index()
        index.append({"file": stored, "name": name, "size": 3,
                      "ts": int(time.time() - age_days * 86400)})
        self.assertTrue(p.save_outbox_index(index))
        return stored

    def test_auto_delete_only_when_enabled_only_old_only_when_idle(self):
        p = self.plugin
        old = self.add_record("old.gcode", 100)
        new = self.add_record("new.gcode", 5)
        # Off by default: nothing happens even for ancient copies.
        self.set_config(OUT, {"auto_delete_days": 30})
        self.assertEqual(p.prune_old_outbox(busy=False), 0)
        self.assertEqual(len(p.load_outbox_index()), 2)
        # Enabled, but an operation is active: skipped entirely.
        self.set_config(OUT, {"auto_delete": True, "auto_delete_days": 90})
        self.assertEqual(p.prune_old_outbox(busy=True), 0)
        self.assertEqual(len(p.load_outbox_index()), 2)
        # Enabled and idle: only the copy older than N days goes.
        self.assertEqual(p.prune_old_outbox(busy=False), 1)
        self.assertEqual([r["file"] for r in p.load_outbox_index()], [new])
        self.assertFalse((pathlib.Path(p.OUTBOX_DIR) / old).exists())
        self.assertTrue((pathlib.Path(p.OUTBOX_DIR) / new).exists())
        # N is respected (30 days removes the 5 day old copy only after it ages).
        self.set_config(OUT, {"auto_delete": True, "auto_delete_days": 365})
        self.assertEqual(p.prune_old_outbox(busy=False), 0)

    def test_auto_delete_uses_safe_path_and_respects_dashboard_busy_state(self):
        p = self.plugin
        index = [{"file": "../escape.gcode", "ts": 1}, {"file": "unassigned/ok.gcode", "ts": True},
                 {"file": "unassigned/x.gcode", "ts": "1"}]
        self.assertTrue(p.save_outbox_index(index))
        outside = self.root / "escape.gcode"
        outside.write_text("keep", encoding="utf-8")
        self.set_config(OUT, {"auto_delete": True, "auto_delete_days": 30})
        self.assertEqual(p.prune_old_outbox(busy=False), 0)
        self.assertTrue(outside.exists())
        old = self.add_record("busy.gcode", 400)
        dashboard = types.SimpleNamespace(_leftover_blocker=lambda: "busy")
        p._SETTINGS_OWNERS[DASH] = dashboard
        self.assertEqual(p.prune_old_outbox(), 0)
        self.assertTrue((pathlib.Path(p.OUTBOX_DIR) / old).exists())
        dashboard._leftover_blocker = lambda: ""
        self.assertEqual(p.prune_old_outbox(), 1)

    def add_slice(self, name, age, target=None, sid="Bambu", digest=None, exists=True):
        p = self.plugin
        folder = target or "unassigned"
        directory = pathlib.Path(p.OUTBOX_DIR) / folder
        directory.mkdir(parents=True, exist_ok=True)
        if exists:
            (directory / name).write_text(name, encoding="utf-8")
        record = {"file": folder + "/" + name, "name": name, "size": 3,
                  "ts": int(time.time()) - age, "printer_settings_id": sid,
                  "sha256": digest or (name.encode().hex() * 8)[:64].ljust(64, "0")}
        if target:
            record["target_uuid"] = target
        index = p.load_outbox_index()
        index.append(record)
        self.assertTrue(p.save_outbox_index(index))
        return record

    def files(self):
        return sorted(r["name"] for r in self.plugin.load_outbox_index())

    def test_keep_last_is_off_by_default_and_junk_means_all(self):
        p = self.plugin
        for raw in ({}, {"keep_last": 7}, {"keep_last": "3"}, {"keep_last": True}, {"keep_last": -1}):
            self.assertEqual(ps.normalize(OUT, raw)["keep_last"], 0)
            outbox = p.PrintersOutbox()
            self.set_config(OUT, raw)
            for i in range(6):
                self.add_slice("a%d_%s.gcode" % (i, len(str(raw))), 100 - i)
            self.assertEqual(self.execute(outbox)[0], "success")
            self.assertGreaterEqual(len(p.load_outbox_index()), 7)
            p.save_outbox_index([])
            shutil.rmtree(p.OUTBOX_DIR, ignore_errors=True)

    def test_keep_last_three_trims_oldest_within_the_same_printer_only(self):
        p = self.plugin
        for i in range(5):
            self.add_slice("mine%d.gcode" % i, 500 - i * 10, sid="Bambu")
        other = [self.add_slice("other%d.gcode" % i, 900 - i, sid="Voron") for i in range(5)]
        outbox = p.PrintersOutbox()
        self.set_config(OUT, {"keep_last": 3})
        self.assertEqual(self.execute(outbox)[0], "success")
        names = self.files()
        # The new slice is under the preset name "Voron"; Bambu copies are another group.
        self.assertEqual(len([n for n in names if n.startswith("mine")]), 5)
        self.assertEqual(len([n for n in names if n.startswith("other")]) + 1, 3)
        self.assertEqual(sorted(n for n in names if n.startswith("other")), ["other3.gcode", "other4.gcode"])
        removed = [pathlib.Path(p.OUTBOX_DIR, "unassigned", "other%d.gcode" % i) for i in (0, 1, 2)]
        self.assertFalse(any(path.exists() for path in removed))
        self.assertTrue(all(pathlib.Path(p.OUTBOX_DIR, r["file"]).exists() for r in other[3:]))

    def test_keep_last_spares_history_linked_busy_and_missing_blocker(self):
        p = self.plugin
        linked = self.add_slice("linked.gcode", 900)
        for i in range(4):
            self.add_slice("s%d.gcode" % i, 500 - i * 10, sid="Voron")
        self.add_slice("linked2.gcode", 1000, sid="Voron", digest=linked["sha256"])
        p.save_print_history([{"id": "h1", "filename": "x", "sha256": linked["sha256"], "state": "completed",
                               "printer_key": "k", "started_at": 1}])
        self.set_config(OUT, {"keep_last": 3})
        probe = {"printer_settings_id": "Voron"}
        # Active operation: nothing removed.
        p._SETTINGS_OWNERS[DASH] = types.SimpleNamespace(_leftover_blocker=lambda: "busy")
        self.assertEqual(p.prune_outbox_keep_last(3, probe), 0)
        p._SETTINGS_OWNERS[DASH] = types.SimpleNamespace(_leftover_blocker=lambda: "")
        # Five Voron copies, keep 3: the two oldest are candidates, but the
        # oldest is tied to the journal by SHA-256, so only one goes.
        self.assertEqual(p.prune_outbox_keep_last(3, probe), 1)
        names = self.files()
        self.assertIn("linked2.gcode", names)       # linked to the journal by SHA-256
        self.assertIn("linked.gcode", names)        # other group, untouched
        self.assertEqual(len([n for n in names if n.startswith("s")]), 3)
        self.assertFalse((pathlib.Path(p.OUTBOX_DIR) / "unassigned" / "s0.gcode").exists())

    def test_keep_last_never_raises_and_hook_still_succeeds(self):
        p = self.plugin
        outbox = p.PrintersOutbox()
        self.set_config(OUT, {"keep_last": 3})
        with unittest.mock.patch.object(p, "prune_outbox_keep_last", side_effect=RuntimeError("boom")):
            self.assertEqual(self.execute(outbox)[0], "success")
        with unittest.mock.patch.object(p, "outbox_remove", side_effect=OSError("denied")):
            for i in range(5):
                self.add_slice("v%d.gcode" % i, 50 - i, sid="Voron")
            self.assertEqual(p.prune_outbox_keep_last(3, {"printer_settings_id": "Voron"}), 0)

    def test_pruning_timer_starts_and_stops_with_the_runtime(self):
        p = self.plugin
        p.start_outbox_pruning()
        handle = p._PRUNE_STOP
        self.assertIsNotNone(handle)
        self.assertTrue(handle["timer"].daemon)
        p.stop_outbox_pruning()
        self.assertTrue(handle["timer"].finished.is_set())
        self.assertIsNone(p._PRUNE_STOP)
        with unittest.mock.patch.object(p, "prune_old_outbox") as prune:
            p._prune_tick(handle)  # a stale tick after stop does nothing
            prune.assert_not_called()

    # --- Print Farm ----------------------------------------------------------
    def printing_status(self):
        return {"state": "printing", "filename": "cube.gcode", "progress": 10}

    def test_history_off_records_nothing_and_keeps_existing(self):
        p = self.plugin
        printer = {"id": 1, "url": "http://v.local", "name": "V", "type": "moonraker"}
        self.set_config(DASH, {})
        self.assertTrue(p.record_print_observation(printer, self.printing_status()))
        self.assertEqual(len(p.load_print_history()), 1)
        before = (self.root / "print_history.json").read_text(encoding="utf-8")
        self.set_config(DASH, {"keep_history": False})
        other = dict(printer, id=2, url="http://w.local")
        self.assertFalse(p.record_print_observation(other, self.printing_status()))
        self.assertFalse(p.record_print_observation(
            printer, {"state": "complete", "filename": "cube.gcode"}))
        self.assertIsNone(p.record_print_started(other, "x.gcode"))
        self.assertIsNone(p.record_orca_job(None, "job", "sent"))
        self.assertEqual((self.root / "print_history.json").read_text(encoding="utf-8"), before)

    def test_page_settings_for_the_panel(self):
        p = self.plugin
        dashboard = p.PrintersDashboard()
        self.set_config(DASH, {"poll_seconds": 5, "pause_hidden": False})
        self.assertEqual(dashboard._page_settings(), {"pollSeconds": 5, "pauseHidden": False})
        self.set_config(DASH, "garbage")
        self.assertEqual(dashboard._page_settings(), {"pollSeconds": 15, "pauseHidden": True})

    def test_panel_page_uses_configurable_interval_and_visibility(self):
        page = self.plugin.PAGE
        self.assertIn("applyPollSettings", page)
        self.assertIn("document.hidden", page)
        self.assertIn("visibilitychange", page)
        self.assertNotIn("}, 15000);", page)

    # --- clear history by one-time token --------------------------------------
    def seed_history(self, count=2):
        p = self.plugin
        for n in range(count):
            p.record_orca_job(None, "job%d" % n, "sent")
        self.assertEqual(len(p.load_print_history()), count)

    def test_token_clears_once_and_repeat_does_not_clear_again(self):
        p = self.plugin
        self.seed_history()
        self.set_config(DASH, {"clear_history_token": "tok-1"})
        self.assertTrue(p.apply_history_clear_request())
        self.assertEqual(p.load_print_history(), [])
        p.record_orca_job(None, "after", "sent")
        for _ in range(3):
            self.assertFalse(p.apply_history_clear_request())
        self.assertEqual([r["filename"] for r in p.load_print_history()], ["after"])
        # Survives a process restart: the processed token lives in plugin storage.
        p._HISTORY_TOKEN_SEEN = None
        self.assertFalse(p.apply_history_clear_request())
        self.assertEqual(len(p.load_print_history()), 1)
        # A new click means a new token.
        self.set_config(DASH, {"clear_history_token": "tok-2"})
        self.assertTrue(p.apply_history_clear_request())
        self.assertEqual(p.load_print_history(), [])

    def test_no_token_or_default_config_never_clears(self):
        p = self.plugin
        self.seed_history()
        self.set_config(DASH, {})
        self.assertFalse(p.apply_history_clear_request())
        self.set_config(DASH, {"clear_history_token": ""})
        self.assertFalse(p.apply_history_clear_request())
        self.assertEqual(len(p.load_print_history()), 2)

    def test_damaged_history_is_not_overwritten_and_request_is_consumed(self):
        p = self.plugin
        store = self.root / "print_history.json"
        original = '[{"id":"good"}, null]'
        store.write_text(original, encoding="utf-8")
        self.set_config(DASH, {"clear_history_token": "tok-d"})
        self.assertFalse(p.apply_history_clear_request())
        self.assertEqual(store.read_text(encoding="utf-8"), original)
        store.write_text(json.dumps([{"id": "1", "state": "sent"}]), encoding="utf-8")
        self.assertFalse(p.apply_history_clear_request())  # not cleared later by surprise
        self.assertEqual(len(p.load_print_history()), 1)

    def test_failed_token_mark_does_not_clear(self):
        p = self.plugin
        self.seed_history()
        self.set_config(DASH, {"clear_history_token": "tok-f"})
        with unittest.mock.patch.object(p, "write_bytes_atomic", side_effect=OSError("disk")):
            self.assertFalse(p.apply_history_clear_request())
        self.assertEqual(len(p.load_print_history()), 2)

    def test_global_history_view_applies_pending_clear_first(self):
        p = self.plugin
        dashboard = p.PrintersDashboard()
        posted = []
        dashboard._post = posted.append
        self.seed_history()
        self.set_config(DASH, {"clear_history_token": "tok-v"})
        dashboard._post_print_history_all({"requestId": 1})
        self.assertEqual(posted[-1]["printHistory"]["items"], [])


class SettingsPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plugin = _load_plugin()

    def pages(self):
        p = self.plugin
        return {OUT: p.PrintersOutbox().get_config_ui(), DASH: p.PrintersDashboard().get_config_ui()}

    def test_pages_are_self_contained_html_with_localized_labels_and_no_secrets(self):
        p = self.plugin
        for language in ("en", "ru", "ja", "de"):
            p._CACHED_UI_LANGUAGE = language
            for kind, html in self.pages().items():
                self.assertTrue(html.strip().startswith("<!DOCTYPE html>"))
                self.assertNotRegex(html, r"(?i)(src|href)\s*=\s*[\"']?(https?:|//)")
                self.assertNotRegex(html, r"(?i)url\(\s*[\"']?https?:")
                self.assertNotRegex(html, r"(?i)(api[_-]?key|access[_-]?code|password|secret)")
                spec = json.loads(re.search(r"var spec = (\{.*?\});\n", html, re.S).group(1))
                self.assertTrue(spec["fields"])
                for field in spec["fields"]:
                    self.assertTrue(field["label"].strip())
                    self.assertNotIn("cfg", field["label"])
                self.assertEqual(spec["defaults"], ps.defaults(kind))
                self.assertEqual("clear" in spec, kind == DASH)
        p._CACHED_UI_LANGUAGE = "ru"
        self.assertIn(p.ui_text("cfgKeepCopies"), self.pages()[OUT])
        self.assertIn("Сохранять копии нарезок", self.pages()[OUT])
        self.assertIn("Очистить журнал печати? Действие необратимо.", self.pages()[DASH])
        p._CACHED_UI_LANGUAGE = "en"

    def test_every_setting_has_a_control_and_no_forbidden_option(self):
        pages = self.pages()
        specs = {k: json.loads(re.search(r"var spec = (\{.*?\});\n", v, re.S).group(1))
                 for k, v in pages.items()}
        self.assertEqual([f["key"] for f in specs[OUT]["fields"]],
                         ["keep_copies", "journal_orca_events", "profile_hint",
                          "auto_delete", "auto_delete_days", "keep_last"])
        self.assertEqual([f["key"] for f in specs[DASH]["fields"]],
                         ["keep_history", "poll_seconds", "pause_hidden"])
        poll = specs[DASH]["fields"][1]
        self.assertEqual([o[0] for o in poll["options"]], [5, 15, 30, 60])
        age = specs[OUT]["fields"][4]
        self.assertEqual([o[0] for o in age["options"]], [30, 90, 180, 365])
        self.assertEqual(age["requires"], "auto_delete")
        keep = specs[OUT]["fields"][5]
        self.assertEqual([o[0] for o in keep["options"]], [0, 3, 5, 10, 20])
        self.assertNotIn("requires", keep)

    def test_every_new_label_exists_in_all_23_catalogs(self):
        p = self.plugin
        keys = [k for k in p.UI_COPY["en"] if k.startswith("cfg")] + ["outboxCopiesOff"]
        self.assertGreaterEqual(len(keys), 20)
        self.assertEqual(len(p.UI_COPY), 23)
        for locale, catalog in p.UI_COPY.items():
            for key in keys:
                self.assertTrue(catalog.get(key, "").strip(), (locale, key))

    def test_page_renders_and_saves_through_a_node_window_orca_stub(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        runner = ROOT / "test_settings_page.js"
        for kind, html in self.pages().items():
            script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
            with tempfile.TemporaryDirectory(prefix="pf-page-") as temp:
                path = pathlib.Path(temp) / "page.js"
                path.write_text(script, encoding="utf-8")
                env = dict(os.environ, TEMP=temp, TMP=temp)
                done = subprocess.run([node, str(runner), str(path), kind],
                                      capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
