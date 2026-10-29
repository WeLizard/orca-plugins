"""Connection routing, explicit camera ownership and secret/lifecycle guards."""
import json
import time
import types
from unittest import mock

import pytest
import credential_store
from test_credential_store import FakeProvider
from test_printers_plugin import _load_plugin, CameraOverlayUiTest, _FAKE_DOM_JS, _page_slice


@pytest.fixture
def setup():
    p = _load_plugin()
    p._pf_credentials = credential_store
    provider = FakeProvider()
    p._CREDENTIAL_VAULT = credential_store.Vault(lambda: provider)
    p.CAMERA_WORKER.activate()
    page = p.PrintersDashboard()
    page._post = mock.Mock(); page._refresh_worker = mock.Mock()
    c = page._add_manual({"type": "moonraker", "name": "Synthetic", "url": "http://synthetic.invalid:7125/api",
                          "source": "manual", "apikey": "synthetic-key"})
    msg = {"targetUuid": c["target_uuid"], "ownerScope": c["owner_scope_uuid"],
           "targetSelector": c["target_selector"], "bindingRevision": c["binding_revision"],
           "url": c["url"], "connectionType": c["type"], "requestId": 8}
    try:
        yield p, page, c, msg, provider
    finally:
        page._cancel_u1_camera()
        p.CAMERA_WORKER.shutdown()


def enable(setup):
    p, page, card, msg, provider = setup
    page._save_connection_options(dict(msg, webui_url="http://synthetic.invalid:80/fluidd", camera_mode="snapmaker_u1"))
    card = p.load_manual_printers()[0]
    msg = dict(msg, bindingRevision=card["binding_revision"])
    page._post.reset_mock()
    return p, page, card, msg, provider


def test_webui_and_camera_edits_invalidate_binding_without_rebinding_keys(setup):
    p, page, old, msg, provider = setup
    page._camera_urls[old["target_uuid"]] = "http://synthetic.invalid/stale"
    p, page, card, current, provider = enable(setup)
    assert card["binding_revision"] == old["binding_revision"] + 1
    assert card["url"] == old["url"] and card["credential_refs"] == old["credential_refs"]
    assert p.hydrate_credentials(card)["apikey"] == "synthetic-key"
    assert old["target_uuid"] not in page._camera_urls
    assert p.resolve_printer_target([card], msg) is None
    assert p.printer_webui_url(card) == "http://synthetic.invalid:80/fluidd"
    assert "synthetic-key" not in json.dumps(p.public_printer_record(card))


@pytest.mark.parametrize("url", ["http://other.invalid", "javascript:alert(1)",
                                "http://user:password@synthetic.invalid", "http://synthetic.invalid/\r\n"])
def test_webui_rejects_other_hosts_credentials_or_unsafe_schemes(setup, url):
    p, page, card, msg, provider = setup
    page._save_connection_options(dict(msg, webui_url=url, camera_mode="auto"))
    assert page._post.call_args.args[0]["connectionOptionsResult"]["ok"] is False
    assert p.load_manual_printers()[0]["binding_revision"] == card["binding_revision"]


def test_relative_webcam_uses_explicit_webui_origin_but_absolute_keeps_its_port(setup):
    p, _, _, _, _ = setup
    api = "http://printer.invalid:7125"
    web = "http://printer.invalid:8080/fluidd"
    assert p.validated_camera_url("/webcam/?action=stream", api, web) == "http://printer.invalid:8080/webcam/?action=stream"
    assert p.validated_camera_url("http://printer.invalid:9999/snapshot", api, web) == "http://printer.invalid:9999/snapshot"
    assert p.validated_camera_url("http://other.invalid/snapshot", api, web) == ""


def test_late_generic_camera_lookup_after_route_edit_is_not_published(setup):
    p, page, c, msg, _ = setup
    def lookup(printer):
        cards = p.load_manual_printers(); cards[0]["binding_revision"] += 1
        p.save_manual_printers(cards)
        return {"ok": True, "stream": "http://synthetic.invalid/webcam", "snapshot": ""}
    with mock.patch.object(p, "camera_lookup", side_effect=lookup):
        page._post_camera_info(msg)
    page._post.assert_not_called()
    assert not page._camera_urls


def test_native_camera_requires_explicit_mode_not_name_or_heads(setup):
    p, page, c, msg, _ = setup
    with mock.patch.object(p.CAMERA_WORKER, "submit") as submit:
        page._begin_u1_camera(msg)
    submit.assert_not_called()
    assert page._post.call_args.args[0]["cameraNativeFrame"]["ok"] is False


def test_denied_worker_opens_no_transport_releases_slot_and_can_retry(setup):
    p, page, c, msg, _ = enable(setup)
    with mock.patch.object(p.CAMERA_WORKER, "submit", side_effect=PermissionError("denied")), \
         mock.patch.object(p._pf_u1_camera, "Session") as session:
        page._begin_u1_camera(msg)
    session.assert_not_called()
    assert page._u1_camera is None
    assert page._post.call_args.args[0]["cameraNativeFrame"]["code"] == "thread_denied"
    with mock.patch.object(p.CAMERA_WORKER, "submit") as submit:
        page._begin_u1_camera(dict(msg, requestId=9))
        submit.assert_called_once()
        # Execute the accepted callback with a session that does no networking.
        client = mock.Mock(id="synthetic-session")
        with mock.patch.object(p._pf_u1_camera, "Session", return_value=client):
            submit.call_args.args[0]()
        client.run.assert_called_once()
        client.cancel.assert_called()
    assert page._u1_camera is None and not p._U1_CAMERA_CLIENTS


def test_denied_vault_never_constructs_camera_transport(setup):
    p, page, c, msg, provider = enable(setup)
    provider.deny = True; p._CREDENTIAL_VAULT.clear()
    with mock.patch.object(p.CAMERA_WORKER, "submit", side_effect=lambda fn: fn()), \
         mock.patch.object(p._pf_u1_camera, "Session") as session:
        page._begin_u1_camera(msg)
    session.assert_not_called()
    assert page._post.call_args.args[0]["cameraNativeFrame"]["code"] == "credential_unavailable"
    assert page._u1_camera is None


@pytest.mark.parametrize("change", ["close", "page", "runtime", "binding", "lease"])
def test_queued_camera_invalidated_before_worker_has_no_transport(setup, change):
    p, page, c, msg, _ = enable(setup)
    with mock.patch.object(p.CAMERA_WORKER, "submit") as submit:
        page._begin_u1_camera(msg)
        entry = page._u1_camera
        if change == "close": page.on_close()
        elif change == "page": page._credential_page_epoch += 1
        elif change == "runtime": p._PLUGIN_RUNTIME_EPOCH += 1
        elif change == "binding":
            cards = p.load_manual_printers(); cards[0]["binding_revision"] += 1; p.save_manual_printers(cards)
        else: entry["lease"] = time.monotonic()-1
        page._post.reset_mock()
        with mock.patch.object(p._pf_u1_camera, "Session") as session:
            submit.call_args.args[0]()
    session.assert_not_called(); page._post.assert_not_called()
    assert page._u1_camera is None


def test_duplicate_open_is_one_queued_session(setup):
    p, page, c, msg, _ = enable(setup)
    with mock.patch.object(p.CAMERA_WORKER, "submit") as submit:
        page._begin_u1_camera(msg); page._begin_u1_camera(msg)
        assert submit.call_count == 1
        page._u1_camera["lease"] = time.monotonic()-1
        submit.call_args.args[0]()  # clean up the slot without opening a socket


def test_runtime_shutdown_releases_a_camera_accepted_but_not_started(setup):
    p, page, c, msg, _ = enable(setup)
    p._PLUGIN_RUNTIME_ACTIVE = True
    with mock.patch.object(p.CAMERA_WORKER, "submit") as submit:
        page._begin_u1_camera(msg)
        assert page._u1_camera is not None
        p.stop_plugin_runtime()
    assert page._u1_camera is None and p._U1_CAMERA_OWNER is None
    assert p._U1_CAMERA_SLOT.acquire(blocking=False)
    p._U1_CAMERA_SLOT.release()


def test_feedback_has_only_fixed_selectable_url_and_no_bridge_or_secrets(setup):
    p, _, _, _, _ = setup
    page = p.render_feedback_page()
    assert 'value="https://filamenthub.ru"' in page
    assert "synthetic-key" not in page and "/feedback" not in page
    assert "postMessage" not in page and "orca." not in page and "fetch(" not in page
    assert page.count("<script") == 1 and "src=" not in page
    viewer = p.render_viewer_page("http://synthetic.invalid:80/fluidd")
    assert "frame-src http://synthetic.invalid:80" in viewer and "allow-same-origin" in viewer
    assert 'readonly aria-label="URL"' in viewer and "webbrowser" not in viewer


def test_feedback_corner_keeps_local_bug_icon_and_localized_accessible_action(setup, tmp_path):
    import subprocess
    p, _, _, _, _ = setup
    assert p.PAGE.index('id="feedback-corner"') > p.PAGE.index('id="grid"')
    assert 'width:34px; height:34px' in p.PAGE and '#feedback:focus-visible' in p.PAGE
    markup = p.PAGE.split('<button id="feedback"', 1)[1].split('</button>', 1)[0]
    assert '<svg' in markup and '<path d="M12 20v-9"' in markup
    assert 'aria-hidden="true"' in markup and '<img' not in markup
    script = (_FAKE_DOM_JS + "function t(k){return 'Localized feedback';}\n"
        "document.getElementById('feedback').innerHTML='<svg>local bug</svg>';\n" +
        _page_slice('// <connection-options>', '// </connection-options>') +
        "var b=document.getElementById('feedback');b.fire('click');\n"
        "console.log(JSON.stringify({html:b.innerHTML,title:b.title,aria:b.getAttribute('aria-label'),posted:posted}));")
    path = tmp_path / 'feedback-icon.js'; path.write_text(script, encoding='utf-8')
    done = subprocess.run(['node', str(path)], capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert done.returncode == 0, done.stderr
    value = json.loads(done.stdout)
    assert value['html'] == '<svg>local bug</svg>'
    assert value['title'] == value['aria'] == 'Localized feedback'
    assert value['posted'] == [{'type':'feedback'}]


def test_native_overlay_rejects_old_session_and_clears_expired_image():
    case = CameraOverlayUiTest()
    value = case.run_js(r'''
      openCamera(Object.assign({},P1,{camera_mode:'snapmaker_u1'}),opener);
      var id=camState.requestId;
      onCameraNativeFrame({ok:true,started:true,requestId:id,targetUuid:'u1',sessionId:'current'});
      var frame={ok:true,requestId:id,targetUuid:'u1',sessionId:'old',sequence:1,
        received_at:Date.now()/1000,captured_at:null,image:'data:image/jpeg;base64,/9j/2Q=='};
      onCameraNativeFrame(frame);var stale=img.src;
      frame.sessionId='current';onCameraNativeFrame(frame);var shown=img.src;
      advance(10001);report({stale:stale||'',shown:shown||'',note:note.textContent});
    ''')
    assert value["stale"] == "" and value["shown"].startswith("data:image/jpeg;base64,")
    assert value["src"] in (None, "") and "cameraUnavailable" in value["note"]


@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_native_failure_is_terminal_for_late_started_frame_and_error(failure):
    case = CameraOverlayUiTest()
    trigger = ("advance(10001);" if failure == "timeout" else
        "onCameraNativeFrame({ok:false,code:'thread_denied',requestId:id,targetUuid:'u1'});")
    value = case.run_js(r'''
      openCamera(Object.assign({},P1,{camera_mode:'snapmaker_u1'}),opener);
      var id=camState.requestId;
      var started={ok:true,started:true,requestId:id,targetUuid:'u1',sessionId:'session'};
      onCameraNativeFrame(started);
      var frame={ok:true,requestId:id,targetUuid:'u1',sessionId:'session',sequence:1,
        received_at:Date.now()/1000,captured_at:null,image:'data:image/jpeg;base64,/9j/2Q=='};
      onCameraNativeFrame(frame);
    ''' + trigger + r'''
      var terminalNote=note.textContent, sent=posted.length;
      onCameraNativeFrame(started);
      frame.sequence=2;onCameraNativeFrame(frame);
      onCameraNativeFrame({ok:false,code:'unavailable',requestId:id,targetUuid:'u1'});
      advance(6000);
      report({terminalNote:terminalNote,failed:camState.failed,
        lateMessages:posted.length-sent,sessionId:camState.sessionId});
    ''')
    assert value["failed"] is True and value["sessionId"] is None
    assert value["src"] in (None, "") and value["note"] == value["terminalNote"]
    assert value["lateMessages"] == 0
