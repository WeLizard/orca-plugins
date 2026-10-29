"""Credential boundary tests use only an injected in-memory provider."""
import json
import threading
import unittest
from unittest import mock

import credential_store as store
from test_printers_plugin import _load_plugin

class FakeProvider:
    def __init__(self):
        self.values = {}
        self.reads = 0
        self.writes = 0
        self.deny = False
        self.fail_write = 0
        self.corrupt = False
    def set_password(self, service, user, value):
        self.writes += 1
        if self.deny or self.writes == self.fail_write:
            raise RuntimeError('synthetic refusal')
        self.values[service, user] = value
    def get_password(self, service, user):
        self.reads += 1
        if self.deny:
            raise RuntimeError('synthetic refusal')
        return 'wrong' if self.corrupt else self.values.get((service, user))

class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.plugin = _load_plugin()
        self.provider = FakeProvider()
        self.plugin._CREDENTIAL_VAULT = store.Vault(lambda: self.provider)
        # Exception identity must be the same as the embedded runtime helper.
        self.plugin._pf_credentials = store
        self.page = self.plugin.PrintersDashboard()
    def card(self, **values):
        return dict({'name':'Synthetic printer', 'url':'http://synthetic.invalid/Base',
                     'type':'octoprint', 'source':'manual'}, **values)
    def saved(self, **values):
        result = self.page._add_manual(self.card(apikey='synthetic-key', **values))
        self.assertIsNotNone(result)
        return result
    def raw(self):
        with open(self.plugin.PRINTERS_FILE, encoding='utf-8') as f:
            return f.read()
    def test_save_writes_only_verified_immutable_refs(self):
        card = self.saved(access_code='synthetic-code')
        raw = self.raw()
        self.assertNotIn('synthetic-key', raw)
        self.assertNotIn('synthetic-code', raw)
        self.assertTrue(self.plugin.save_manual_printers([self.plugin.hydrate_credentials(card)]))
        self.assertNotIn('synthetic-key', self.raw())
        self.assertEqual(len(self.provider.values), 2)
        hydrated = self.plugin.hydrate_credentials(card)
        self.assertEqual(hydrated['apikey'], 'synthetic-key')
        self.assertEqual(hydrated['access_code'], 'synthetic-code')
        self.assertNotIn('credential_refs', self.plugin.public_printer_record(card))
        self.assertNotIn('synthetic-key', json.dumps(self.plugin.public_printer_record(hydrated)))
    def test_bambu_ref_poll_preserves_serial_and_model_without_persisting_secret(self):
        import hashlib, struct
        card = self.page._add_manual(self.card(type='bambu', access_code='synthetic-bambu'))
        certificate = b'synthetic certificate'
        card['tls_pins'] = {'mqtt':hashlib.sha256(certificate).hexdigest()}
        card['tls_trust_revision'] = 1
        self.assertTrue(self.plugin.save_manual_printers([card]))
        topic = b'device/SYNTHETIC-SERIAL/report'
        report = json.dumps({'print':{'printer_type':'N7','gcode_state':'IDLE','nozzle_temper':21}}).encode()
        packets = iter([(0x20,b'\x00\x00'),(0x90,b'\x00\x01\x00'),
                        (0x30,struct.pack('!H',len(topic))+topic+report)])
        sock, context = mock.MagicMock(), mock.MagicMock()
        sock.getpeercert.return_value = certificate
        context.wrap_socket.return_value = sock
        with mock.patch.object(self.plugin.socket,'create_connection',return_value=sock), \
             mock.patch.object(self.plugin,'ssl_ctx_for',return_value=context), \
             mock.patch.object(self.plugin,'_mqtt_read_packet',side_effect=lambda *args:next(packets)):
            status = self.plugin.poll_printer(card)
        self.assertTrue(status['online'])
        stored = self.plugin.load_manual_printers()[0]
        self.assertEqual(stored['target_uuid'],card['target_uuid'])
        self.assertEqual(stored['serial'],'SYNTHETIC-SERIAL')
        self.assertEqual(stored['serial_source'],'mqtt')
        self.assertEqual(stored['model_code'],'N7')
        self.assertEqual(card['serial'],'SYNTHETIC-SERIAL')
        self.assertNotIn('access_code',card)
        self.assertNotIn('synthetic-bambu',self.raw())
        self.assertNotIn('synthetic-bambu',json.dumps(self.plugin.public_printer_record(card)))

    def test_store_load_and_metadata_save_never_access_provider(self):
        card = self.saved()
        self.provider.deny = True
        reads = self.provider.reads
        loaded = self.plugin.load_manual_printers()
        loaded[0]['name'] = 'Metadata only'
        self.assertTrue(self.plugin.save_manual_printers(loaded))
        self.assertEqual(self.provider.reads, reads)
        self.assertEqual(loaded[0]['credential_refs'], card['credential_refs'])
    def test_partial_write_failure_keeps_original_json(self):
        self.saved()
        original = self.raw()
        self.provider.fail_write = self.provider.writes + 2
        self.assertIsNone(self.page._add_manual(self.card(apikey='new-key', access_code='new-code')))
        self.assertEqual(self.raw(), original)
    def test_readback_failure_keeps_original_json(self):
        self.saved(); original = self.raw(); self.provider.corrupt = True
        self.assertIsNone(self.page._add_manual(self.card(apikey='new-key')))
        self.assertEqual(self.raw(), original)
    def test_replace_failure_keeps_original_json(self):
        self.saved(); original = self.raw()
        with mock.patch.object(self.plugin.os, 'replace', side_effect=OSError('synthetic failure')):
            self.assertIsNone(self.page._add_manual(self.card(apikey='new-key')))
        self.assertEqual(self.raw(), original)
    def test_wrong_root_target_endpoint_selector_or_ref_fail_closed(self):
        card = self.saved()
        for key, value in [('target_uuid','another'), ('owner_scope_uuid','another'),
                           ('url','http://synthetic.invalid/base'), ('target_selector','another'),
                           ('type','moonraker')]:
            with self.subTest(key=key):
                with self.assertRaises(store.Unavailable):
                    self.plugin.hydrate_credentials(dict(card, **{key:value}))
        with mock.patch.object(self.plugin, 'PLUGIN_STORAGE_DIR', 'F:/Temp/synthetic-other-root'):
            with self.assertRaises(store.Unavailable): self.plugin.hydrate_credentials(card)
        invalid = dict(card, credential_refs={'apikey':{'id':'not-uuid','binding':'not-service'}})
        with self.assertRaises(store.Unavailable): self.plugin.hydrate_credentials(invalid)
    def test_missing_or_refused_vault_blocks_network_and_prompts_once(self):
        card = self.saved(); self.provider.deny = True
        before = self.provider.reads
        with mock.patch.object(self.plugin, '_get_json') as network:
            for _ in range(3):
                status = self.plugin.poll_printer(card)
                self.assertEqual(status['error_code'], 'credential_unavailable')
            self.assertFalse(self.plugin.camera_lookup(card)['ok'])
            self.assertFalse(self.plugin.send_gcode(card,'synthetic.gcode',b'',False)['ok'])
            network.assert_not_called()
        self.assertEqual(self.provider.reads, before + 1)
        self.plugin._CREDENTIAL_VAULT.clear(); self.provider.deny = False
        self.assertEqual(self.plugin.hydrate_credentials(card)['apikey'], 'synthetic-key')
    def test_late_provider_result_is_discarded_without_blocking_clear(self):
        card = self.saved(); entered = threading.Event(); release = threading.Event()
        original = self.provider.get_password
        def delayed(*args):
            entered.set(); release.wait(3); return original(*args)
        self.provider.get_password = delayed
        errors = []
        def hydrate():
            try: self.plugin.hydrate_credentials(card)
            except store.Unavailable: errors.append(True)
        thread = threading.Thread(target=hydrate); thread.start()
        self.assertTrue(entered.wait(2))
        self.plugin._CREDENTIAL_VAULT.clear(); release.set(); thread.join(3)
        self.assertEqual(errors,[True]); self.assertFalse(self.plugin._CREDENTIAL_VAULT.cache)
    def legacy(self):
        card = self.card(apikey='legacy-key', access_code='legacy-code')
        self.plugin._ensure_printer_identity(card)
        self.assertTrue(self.plugin.save_manual_printers([card]))
        return card
    def test_migration_requires_confirmation_and_snapshot(self):
        self.legacy(); original = self.raw()
        self.assertFalse(self.plugin.migrate_credentials(None))
        preview = self.plugin.credential_migration_preview()
        cards = self.plugin.load_manual_printers(); cards[0]['name'] = 'Changed'
        self.plugin.save_manual_printers(cards)
        self.assertFalse(self.plugin.migrate_credentials(preview['confirmation']))
        self.assertIn('legacy-key',self.raw()); self.assertEqual(self.provider.writes,0)
    def test_migration_failure_preserves_all_plaintext_and_success_removes_it(self):
        self.legacy(); original = self.raw(); preview = self.plugin.credential_migration_preview()
        self.provider.fail_write = 2
        self.assertFalse(self.plugin.migrate_credentials(preview['confirmation']))
        self.assertEqual(self.raw(),original)
        self.provider.fail_write = 0
        self.assertTrue(self.plugin.migrate_credentials(preview['confirmation']))
        self.assertNotIn('legacy-key',self.raw()); self.assertNotIn('legacy-code',self.raw())
        self.assertEqual(self.plugin.credential_migration_preview()['count'],0)
    def test_endpoint_change_requires_new_key_and_never_reuses_previous_entry(self):
        card = self.saved(); original = self.raw()
        self.assertIsNone(self.page._add_manual(dict(card,url='http://synthetic.invalid/Other',apikey='')))
        self.assertEqual(self.raw(),original)
        edited = self.page._add_manual(dict(card,url='http://synthetic.invalid/Other',apikey='new-key'))
        self.assertIsNotNone(edited)
        self.assertNotEqual(edited['credential_refs'],card['credential_refs'])
        self.assertEqual(self.plugin.hydrate_credentials(edited)['apikey'],'new-key')
        with self.assertRaises(store.Unavailable): self.plugin.hydrate_credentials(card)

    def test_vault_write_does_not_hold_store_lock_for_add_or_migration(self):
        for migrate in (False, True):
            with self.subTest(migrate=migrate):
                if migrate: self.legacy()
                original_set=self.provider.set_password
                def checked(*args):
                    acquired=[]
                    def read_metadata():
                        success=self.plugin._STORE_LOCK.acquire(timeout=1)
                        acquired.append(success)
                        if success:
                            self.plugin.load_manual_printers()
                            self.plugin._STORE_LOCK.release()
                    thread=threading.Thread(target=read_metadata);thread.start();thread.join(2)
                    self.assertEqual(acquired,[True])
                    original_set(*args)
                self.provider.set_password=checked
                if migrate:
                    preview=self.plugin.credential_migration_preview()
                    self.assertTrue(self.plugin.migrate_credentials(preview['confirmation']))
                else:
                    self.assertIsNotNone(self.page._add_manual(self.card(apikey='synthetic-key')))
                self.provider.set_password=original_set

    def test_snapshot_change_during_provider_write_keeps_store_change(self):
        card = self.saved(); original_set = self.provider.set_password
        def changed(*args):
            original_set(*args)
            cards = self.plugin.load_manual_printers(); cards[0]['name']='Concurrent edit'
            self.plugin.save_manual_printers(cards)
        self.provider.set_password=changed
        self.assertIsNone(self.page._add_manual(dict(card,apikey='new-key')))
        self.assertEqual(self.plugin.load_manual_printers()[0]['name'],'Concurrent edit')
        self.assertNotIn('new-key',self.raw())

    def test_partial_legacy_edit_preserves_other_plaintext_until_migration(self):
        card=self.legacy()
        edited=self.page._add_manual(dict(card,apikey='replacement',access_code=''))
        raw=json.loads(self.raw())[0]
        self.assertEqual(raw['access_code'],'legacy-code')
        self.assertNotIn('apikey',raw)
        self.assertEqual(self.plugin.hydrate_credentials(edited)['access_code'],'legacy-code')

    def test_closed_page_during_native_read_rejects_network_and_does_not_cache(self):
        card=self.saved(); get=self.provider.get_password
        def close(*args):
            self.page.on_close()
            return get(*args)
        self.provider.get_password=close
        epoch=self.page._credential_page_epoch
        guard=lambda:self.page._accept_actions and epoch==self.page._credential_page_epoch
        with mock.patch.object(self.plugin,'_multipart_upload') as network:
            result=self.plugin._run_credential_work(guard,self.plugin.send_gcode,card,'x.gcode',b'',False)
        network.assert_not_called()
        self.assertEqual(result['journal_state'],'rejected')
        self.assertEqual(result['evidence_ref'],'credential:pre_dispatch')
        self.assertFalse(self.plugin._CREDENTIAL_VAULT.cache)

    def test_closed_page_during_native_write_does_not_save_new_card(self):
        setter=self.provider.set_password
        def close(*args):
            setter(*args); self.page.on_close()
        self.provider.set_password=close
        with mock.patch.object(self.plugin.ACTION_WORKER,'submit') as submit:
            self.page._submit_ui_work(self.page._add_manual,self.card(apikey='synthetic-key'))
        work,*args=submit.call_args.args
        self.assertIsNone(work(*args))
        self.assertEqual(self.plugin.load_manual_printers(),[])

    def test_binding_revision_change_during_native_read_blocks_old_action(self):
        card=self.saved(); get=self.provider.get_password
        def edit(*args):
            value=get(*args)
            cards=self.plugin.load_manual_printers(); cards[0]['binding_revision']+=1
            self.plugin.save_manual_printers(cards)
            return value
        self.provider.get_password=edit
        with self.assertRaises(store.Unavailable): self.plugin.hydrate_credentials(card)
        self.assertFalse(self.plugin._CREDENTIAL_VAULT.cache)

    def test_second_bambu_credential_check_is_rejected_before_mqtt(self):
        card=self.page._add_manual(self.card(type='bambu',serial='SYNTHETIC',access_code='synthetic-key'))
        publish=self.plugin._bambu_publish
        def revoked(*args,**kwargs):
            self.plugin._CREDENTIAL_VAULT.clear();self.provider.deny=True
            return publish(*args,**kwargs)
        with mock.patch.object(self.plugin,'status_can_control',return_value=True), \
             mock.patch.object(self.plugin,'_bambu_publish',side_effect=revoked), \
             mock.patch.object(self.plugin.socket,'create_connection') as network:
            result=self.plugin.printer_command(card,'pause',{})
        network.assert_not_called()
        self.assertEqual(result['journal_state'],'rejected')
        self.assertEqual(result['evidence_ref'],'credential:pre_dispatch')

    def test_ready_invalidates_queued_previous_page_credential_work(self):
        with mock.patch.object(self.plugin.ACTION_WORKER,'submit') as submit:
            self.page._submit_ui_work(self.page._add_manual,self.card(apikey='synthetic-key'))
        work,*args=submit.call_args.args
        with mock.patch.object(self.page,'_refresh_async'):
            self.page.on_message({'type':'ready'})
        self.assertIsNone(work(*args));self.assertEqual(self.provider.writes,0)

    def test_deletion_needs_no_vault_access(self):
        card=self.saved(); self.provider.deny=True; before=self.provider.reads
        message={'targetUuid':card['target_uuid'],'ownerScope':card['owner_scope_uuid'],
                 'url':card['url'],'bindingRevision':card['binding_revision'],
                 'targetSelector':card['target_selector'],'connectionType':card['type']}
        self.assertTrue(self.page._remove_manual(message)); self.assertEqual(self.provider.reads,before)
    def test_edit_without_new_key_does_not_migrate_legacy(self):
        card=self.legacy()
        edited=self.page._add_manual(dict(card,name='Renamed',apikey='',access_code=''))
        self.assertEqual(edited['apikey'],'legacy-key'); self.assertEqual(self.provider.writes,0)
    def test_environment_configuration_is_not_used_by_explicit_provider(self):
        # Synthetic backend base deliberately imitates keyring's constructor.
        class Base:
            def __init__(self): self.set_properties_from_env()
            def set_properties_from_env(self): raise AssertionError('environment applied')
        import sys, types
        for platform, module_name, class_name in (
                ('win32','keyring.backends.Windows','WinVaultKeyring'),
                ('darwin','keyring.backends.macOS','Keyring'),
                ('linux','keyring.backends.SecretService','Keyring')):
            with self.subTest(platform=platform):
                fake=types.ModuleType(module_name); setattr(fake,class_name,Base)
                with mock.patch.dict(sys.modules,{module_name:fake}), mock.patch.object(store.sys,'platform',platform):
                    provider=store.system_backend()
                    if platform=='win32': self.assertEqual(provider.persist,'local_machine')
                    if platform=='darwin': self.assertIsNone(provider.keychain)
    def test_missing_dependency_is_honest_and_dashboard_still_loads(self):
        with mock.patch.object(self.plugin._CREDENTIAL_VAULT,'factory',side_effect=ImportError('synthetic')):
            self.assertIsNone(self.page._add_manual(self.card(apikey='synthetic-key')))
        self.assertEqual(self.plugin.load_manual_printers(),[])

if __name__ == '__main__': unittest.main()
