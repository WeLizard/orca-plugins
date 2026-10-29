"""Selection, approval and credential boundaries for Repetier integration."""
import hashlib
import json
import time
import types
from unittest import mock

import pytest
import credential_store
from test_credential_store import FakeProvider
from test_printers_plugin import _load_plugin
from test_repetier_adapter import server
from test_connections_integration import _Base


@pytest.fixture
def plugin():
    p = _load_plugin()
    p._pf_credentials = credential_store
    p._CREDENTIAL_VAULT = credential_store.Vault(lambda: FakeProvider())
    return p


def card(plugin, slug="Alpha", **values):
    result = dict(name="Synthetic printer", type="repetier", url="http://synthetic.invalid/proxy",
                  repetier_slug=slug, source="manual")
    result.update(values)
    return plugin._ensure_printer_identity(result)


def fresh(c):
    return {k: v for k, v in c.items() if k not in {"target_uuid", "owner_scope_uuid", "target_selector", "server_uuid", "binding_revision", "identity_schema"}}


def identity(c):
    return {"targetUuid": c["target_uuid"], "ownerScope": c["owner_scope_uuid"],
            "targetSelector": c["target_selector"], "bindingRevision": c["binding_revision"],
            "url": c["url"], "connectionType": c["type"]}


def live(plugin, **values):
    result = dict(online=True, state="idle", job_state="idle", can_start=True,
                  upload_ready=True, can_control=False, repetier_slug="Alpha",
                  repetier_url="http://synthetic.invalid/proxy")
    result.update(values)
    return plugin._finalize_status(result, "repetier")


def test_two_slugs_one_url_have_different_bindings_and_discovery(plugin):
    first, second = card(plugin), card(plugin, "Beta")
    assert first["target_selector"] != second["target_selector"]
    assert plugin._orca_discovery_ref("Machine", first["url"], "repetier", "Alpha") != \
           plugin._orca_discovery_ref("Machine", first["url"], "repetier", "Beta")
    page = plugin.PrintersDashboard()
    assert page._add_manual(fresh(first)) is not None
    assert page._add_manual(fresh(second)) is not None
    assert len(plugin.load_manual_printers()) == 2


@pytest.mark.parametrize("slug", [None, "", "Wrong"])
def test_missing_or_wrong_slug_no_endpoint_or_name_fallback(plugin, slug):
    c = card(plugin)
    record = {"name": c["name"], "printer_settings_id": c["name"],
              "host_type": "repetier", "match_url": c["url"],
              "match_endpoint": plugin._endpoint_key(c["url"]), "repetier_slug": slug}
    assert plugin.outbox_target_match(record, [c]) is None
    record.pop("host_type")
    assert plugin.outbox_target_match(record, [c]) is None


def test_correct_slug_matches_full_base_path_only(plugin):
    first, second = card(plugin), card(plugin, "Beta")
    record = {"host_type": "repetier", "match_url": first["url"], "repetier_slug": "Beta"}
    assert plugin.outbox_target_match(record, [first, second]) is second
    record["match_url"] = "http://synthetic.invalid/another-proxy"
    assert plugin.outbox_target_match(record, [second]) is None


def test_changed_slug_invalidates_resolver_and_approval(plugin):
    c = card(plugin)
    request = identity(c)
    artifact = types.SimpleNamespace(manifest={"kind": "gcode", "sha256": "f"*64},
                                     metadata={}, plate_entries=())
    original = plugin.PrintersDashboard._start_approval_plan(c, artifact, None, False)
    c["repetier_slug"] = "Beta"
    changed = plugin.PrintersDashboard._start_approval_plan(c, artifact, None, False)
    assert original != changed
    assert plugin.resolve_printer_target([c], request) is None
    c = plugin._ensure_printer_identity(c)
    assert plugin.resolve_printer_target([c], request) is None


def test_slug_bound_vault_entry_denied_before_network(plugin):
    provider = FakeProvider()
    plugin._CREDENTIAL_VAULT = credential_store.Vault(lambda: provider)
    page = plugin.PrintersDashboard()
    c = page._add_manual(fresh(card(plugin, apikey="test-secret")))
    assert c is not None
    provider.deny = True
    plugin._CREDENTIAL_VAULT.clear()
    with mock.patch.object(plugin._pf_repetier, "poll") as poll:
        result = plugin.poll_printer(c)
        assert result["error_code"] == "credential_unavailable"
        poll.assert_not_called()
    changed = dict(c, repetier_slug="Beta")
    with mock.patch.object(plugin._pf_repetier, "upload") as upload:
        assert plugin.send_file_path(changed, "job.gcode", "unused", False,
                                     status=live(plugin))["error_code"] == "credential_unavailable"
        upload.assert_not_called()
    assert credential_store.binding(plugin.PLUGIN_STORAGE_DIR, c, "apikey") != \
           credential_store.binding(plugin.PLUGIN_STORAGE_DIR, changed, "apikey")


def test_non_repetier_vault_digest_unchanged(plugin):
    import os
    c = card(plugin, type="octoprint")
    parts = [os.path.normcase(os.path.realpath(plugin.PLUGIN_STORAGE_DIR)), c.get("owner_scope_uuid"),
             c.get("target_uuid"), c.get("url"), c.get("type"), c.get("target_selector"), "apikey"]
    expected = hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    assert credential_store.binding(plugin.PLUGIN_STORAGE_DIR, c, "apikey") == expected


def test_changed_slug_requires_new_typed_key(plugin):
    provider = FakeProvider()
    plugin._CREDENTIAL_VAULT = credential_store.Vault(lambda: provider)
    page = plugin.PrintersDashboard()
    saved = page._add_manual(fresh(card(plugin, apikey="old-key")))
    updated = dict(saved, repetier_slug="Beta")
    assert page._add_manual(updated) is None
    updated["apikey"] = "new-key"
    changed = page._add_manual(updated)
    assert changed and changed["binding_revision"] == saved["binding_revision"] + 1
    assert changed["target_selector"] != saved["target_selector"]
    assert plugin.hydrate_credentials(changed)["apikey"] == "new-key"


def test_status_backend_and_control_capability_fail_closed(plugin):
    c = card(plugin)
    assert plugin.status_can_start(live(plugin), c)
    assert not plugin.status_can_start(live(plugin, can_start=False), c)
    assert not plugin.status_can_upload(live(plugin, upload_ready=False), c)
    printing = live(plugin, state="printing", job_state="printing", can_control=True)
    assert plugin.status_can_control(printing, "pause", c)
    assert not plugin.status_can_control(printing, "resume", c)
    assert not plugin.status_can_control(dict(printing, can_control=False), "cancel", c)
    assert not plugin.status_can_control(dict(printing, observed_at=time.time()-60), "cancel", c)
    assert not plugin.status_can_start(live(plugin, repetier_slug="Beta"), c)
    assert not plugin.status_can_upload(live(plugin, repetier_url="http://synthetic.invalid/other"), c)


def test_send_passes_exact_selected_slug_and_keeps_submission_unverified(plugin, tmp_path):
    c = card(plugin, "Beta")
    path = tmp_path / "job.gcode"; path.write_bytes(b"G28\n")
    with mock.patch.object(plugin._pf_repetier, "upload", return_value={"ok": True, "verified": False}) as upload:
        result = plugin.send_file_path(c, path.name, str(path), True, status=live(plugin, repetier_slug="Beta"))
        assert result == {"ok": True, "verified": False}
        assert upload.call_count == 1
        assert upload.call_args.kwargs["slug"] == "Beta" and upload.call_args.kwargs["start"] is True
        assert callable(upload.call_args.kwargs["is_current"])


def test_adapter_rejection_preserves_journal_unknown_outcome(plugin, tmp_path):
    c = card(plugin)
    with mock.patch.object(plugin._pf_repetier, "upload", return_value={
            "ok": False, "error_code": "unknown_outcome", "outcome": "unknown"}):
        result = plugin.send_file_path(c, "job.gcode", "unused", True, status=live(plugin))
    assert result["journal_state"] == "unknown_outcome"


def test_selection_rechecked_before_saving(plugin):
    page = plugin.PrintersDashboard()
    page._post = mock.Mock(); page._post_status = mock.Mock(); page._refresh_worker = mock.Mock()
    with mock.patch.object(plugin.ACTION_WORKER, "current_job_is_active", return_value=True), \
         mock.patch.object(plugin._pf_repetier, "list_printers", return_value={"ok": True, "printers": []}), \
         mock.patch.object(page, "_add_manual") as add:
        page._add_and_refresh(card(plugin), [])
        add.assert_not_called()
        assert page._post.call_args.args[0]["adoptResult"]["ok"] is False


def test_slice_identity_reads_slug_as_string_not_tcp_port(plugin):
    values = {"host_type": "repetier", "printhost_port": "Office_Printer", "print_host": "http://server/proxy"}
    ctx = types.SimpleNamespace(config_value=lambda key: values.get(key, ""))
    found = plugin._slice_identity(ctx)
    assert found["printhost_port"] == "Office_Printer"
    assert found["host_type"] == "repetier"


def test_profile_compatibility_needs_slug_in_reference(plugin):
    c = card(plugin, source="orca")
    c["discovery_ref"] = plugin._orca_discovery_ref("Profile", c["url"], "repetier", "Alpha")
    artifact = types.SimpleNamespace(manifest={"kind": "gcode"}, metadata={"printer_settings_id": "Profile", "print_host": c["url"],
        "printhost_port": "Alpha", "host_type": "repetier"}, plate_entries=())
    assert plugin.start_compatibility(c, artifact, None)[0] == "compatible"
    c["repetier_slug"] = "Beta"
    assert plugin.start_compatibility(c, artifact, None)[0] == "unknown"


@pytest.mark.parametrize("changes", [{"printhost_port": "Beta"}, {"print_host": "http://other.invalid/proxy"},
    {"print_host": "http://synthetic.invalid/other"}, {"host_type": "octoprint"},
    {"printhost_port": None}, {"print_host": None}, {"host_type": None}])
def test_reused_profile_name_does_not_override_slice_target(plugin, changes):
    c = card(plugin, source="orca")
    c["discovery_ref"] = plugin._orca_discovery_ref("Profile", c["url"], "repetier", "Alpha")
    metadata = {"printer_settings_id": "Profile", "print_host": c["url"],
                "printhost_port": "Alpha", "host_type": "repetier"}
    metadata.update(changes)
    artifact = types.SimpleNamespace(manifest={"kind": "gcode"}, metadata=metadata, plate_entries=())
    assert plugin.start_compatibility(c, artifact, None)[0] == "unknown"


@pytest.mark.parametrize("key", ["я" * 257, "bad\r\nkey"])
def test_invalid_list_key_returns_targeted_failure_without_network(plugin, key):
    page = plugin.PrintersDashboard(); page._post = mock.Mock()
    with mock.patch.object(plugin._pf_repetier, "list_printers") as listing:
        page._load_repetier_printers({"requestId": 13, "url": "http://unused.invalid", "apikey": key})
    listing.assert_not_called()
    assert page._post.call_args.args[0] == {"repetierPrinters": {"ok": False, "requestId": 13}}


def test_list_thread_denial_is_targeted_and_same_form_can_retry(plugin):
    page = plugin.PrintersDashboard(); page._post = mock.Mock()
    msg = {"type": "repetier-list", "url": "http://unused.invalid", "apikey": "key", "requestId": 7}
    with mock.patch.object(plugin.ACTION_WORKER, "submit", side_effect=PermissionError("denied")):
        page.on_message(msg)
    assert {"repetierPrinters": {"ok": False, "requestId": 7}} in [c.args[0] for c in page._post.call_args_list]
    with mock.patch.object(plugin.ACTION_WORKER, "submit", side_effect=lambda fn, *args, **kw: fn(*args, **kw)), \
         mock.patch.object(plugin._pf_repetier, "list_printers", return_value={"ok": True, "printers": []}) as listing:
        page.on_message(dict(msg, requestId=8))
    listing.assert_called_once()
    assert page._post.call_args.args[0] == {"repetierPrinters": {"ok": True, "printers": [], "requestId": 8}}


def test_list_exception_returns_failure_but_stale_page_stays_silent(plugin):
    page = plugin.PrintersDashboard(); page._post = mock.Mock()
    msg = {"requestId": 2, "url": "http://unused.invalid", "apikey": "key"}
    with mock.patch.object(plugin._pf_repetier, "list_printers", side_effect=RuntimeError("private diagnostic")):
        page._load_repetier_printers(msg)
    assert page._post.call_args.args[0] == {"repetierPrinters": {"ok": False, "requestId": 2}}
    old = page._credential_page_epoch
    page.on_close(); page._post.reset_mock()
    page._repetier_list_failure(msg, old)
    page._post.assert_not_called()


def test_queued_tls_probe_page_cycle_does_not_leave_pending_or_release_new_probe(plugin):
    page = plugin.PrintersDashboard(); queued = []
    with mock.patch.object(plugin.ACTION_WORKER, "submit", side_effect=lambda fn, *a, **kw: queued.append(fn)), \
         mock.patch.object(page, "_refresh_async"):
        page._submit_tls_work(mock.Mock(), {"protocol": "mqtt"})
        old_pending = page._tls_pending_id
        # Retain the inner old run: even its late finally must not clear a new id.
        old_run = queued[0].__wrapped__
        page.on_close()
        # The host reopens the page (_open/on_load re-enables actions).
        page._accept_actions = True
        page.on_message({"type": "ready"})
        page._submit_tls_work(mock.Mock(), {"protocol": "mqtt"})
        new_pending = page._tls_pending_id
        assert new_pending and new_pending != old_pending
        queued[0]()  # outer page guard skips stale work
        assert page._tls_pending_id == new_pending
        old_run()  # late old release compares ids
        assert page._tls_pending_id == new_pending
        queued[-1]()
        assert page._tls_pending_id is None


@pytest.fixture
def workflow():
    harness = _Base()
    harness.setUp()
    try:
        yield harness
    finally:
        harness.doCleanups()


def test_complete_start_workflow_requires_nonce_and_sends_one_post(workflow, server):
    w = workflow
    printer = w.add_printer(type="repetier", url=server.base, repetier_slug="Beta", apikey="test-key")
    w.stage_gcode()
    w.run_submit(printer, start=True, grants=[{"targetUuid": printer["target_uuid"], "nonce": "forged",
                                            "bedClear": True, "operatorChecked": True}])
    assert not [r for r in server.requests if r[0] == "POST"]
    _, grants = w.approve_start(printer)
    w.run_submit(printer, start=True, grants=grants)
    posts = [r for r in server.requests if r[0] == "POST"]
    assert len(posts) == 1 and posts[0][1] == "/proxy/printer/job/Beta"
    assert w.operations()[0]["state"] == "submitted"
    # Successful HTTP submission must never create a physical print record.
    assert not [r for r in w.plugin.load_print_history() if r.get("state") == "printing"]
    w.run_submit(printer, start=True, grants=grants)
    assert len([r for r in server.requests if r[0] == "POST"]) == 1


def test_changed_target_after_approval_never_posts(workflow, server):
    w = workflow
    printer = w.add_printer(type="repetier", url=server.base, repetier_slug="Alpha", apikey="test-key")
    w.stage_gcode()
    _, grants = w.approve_start(printer)
    changed = dict(printer, repetier_slug="Beta", apikey="replacement-key")
    assert w.dashboard._add_manual(changed)
    w.run_submit(printer, start=True, grants=grants)
    assert not [r for r in server.requests if r[0] == "POST"]


def test_selected_slug_disappearing_before_dispatch_never_posts(workflow, server):
    w = workflow
    printer = w.add_printer(type="repetier", url=server.base, repetier_slug="Beta", apikey="test-key")
    w.stage_gcode()
    _, grants = w.approve_start(printer)
    server.printers = [p for p in server.printers if p["slug"] != "Beta"]
    w.run_submit(printer, start=True, grants=grants)
    assert not [r for r in server.requests if r[0] == "POST"]


def test_upload_only_complete_workflow_never_uses_job_endpoint(workflow, server):
    w = workflow
    printer = w.add_printer(type="repetier", url=server.base, repetier_slug="Alpha", apikey="test-key")
    w.stage_gcode()
    w.run_submit(printer, start=False)
    posts = [r for r in server.requests if r[0] == "POST"]
    assert len(posts) == 1 and posts[0][1] == "/proxy/printer/model/Alpha"
    assert b"autostart" not in posts[0][3]
    assert w.operations()[0]["state"] == "submitted"


@pytest.mark.parametrize("action", ["start", "pause"])
@pytest.mark.parametrize("change", ["close", "rebind"])
def test_lifecycle_change_during_adapter_preflight_sends_no_mutation(workflow, server, action, change):
    w = workflow
    printer = w.add_printer(type="repetier", url=server.base, repetier_slug="Alpha", apikey="key")
    if action == "start":
        w.stage_gcode(); _, grants = w.approve_start(printer)
    else:
        server.printers[0].update(job="part.gcode", jobid=4, paused=False, pauseState=0)
    real_request = w.plugin._pf_repetier._request
    state_reads = 0
    def request(*args, **kwargs):
        nonlocal state_reads
        value = real_request(*args, **kwargs)
        if "a=stateList" in args[3]:
            state_reads += 1
            if state_reads == 2:
                if change == "close":
                    w.dashboard.on_close()
                else:
                    cards = w.plugin.load_manual_printers()
                    cards[0]["repetier_slug"] = "Beta"
                    cards[0]["binding_revision"] += 1
                    w.plugin._ensure_printer_identity(cards[0])
                    assert w.plugin.save_manual_printers(cards)
        return value
    with mock.patch.object(w.plugin._pf_repetier, "_request", side_effect=request):
        if action == "start":
            w.run_submit(printer, start=True, grants=grants)
        else:
            w.dashboard._submit_target_action(w.dashboard._do_printer_cmd,
                dict(w.identity(printer), action="pause"), "pause")
            scheduler = w.dashboard._action_scheduler
            if scheduler:
                assert scheduler.wait_for_idle(15)
    assert state_reads == 2
    assert not [r for r in server.requests if r[0] == "POST"]
    assert w.operations()[0]["state"] == "rejected"
