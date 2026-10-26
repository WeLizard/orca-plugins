// Renders a Print Farm settings page script against a minimal DOM and a
// window.orca stub (getConfig / saveConfig / onConfig), then drives it.
// Usage: node test_settings_page.js <script.js> <outbox|dashboard>
const fs = require('fs');
const assert = require('assert');

const ids = {};
let active = null;
class El {
  constructor(tag) {
    this.tag = tag; this.attrs = {}; this.children = []; this.listeners = {};
    this._text = ''; this.hidden = false; this.checked = false; this.value = ''; this.disabled = false;
  }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') ids[v] = this; if (k === 'value') this.value = String(v); }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  focus() { active = this; }
  fire(type, ev) { (this.listeners[type] || []).forEach(f => f(Object.assign({ preventDefault() {}, key: '' }, ev))); }
}
const form = new El('div'); ids.form = form;
global.document = {
  getElementById: id => ids[id] || null,
  createElement: tag => new El(tag),
  get activeElement() { return active; },
};
const saves = [];
let pushConfig = null;
global.window = {
  orca: {
    saveConfig: cfg => saves.push(JSON.parse(JSON.stringify(cfg))),
    getConfig: () => ({}),
    onConfig: cb => { pushConfig = cb; cb({}); },
  },
};
global.window.document = global.document;

const [, , scriptPath, kind] = process.argv;
eval(fs.readFileSync(scriptPath, 'utf8'));

const rows = form.children;
const labels = [];
const walk = n => { labels.push(n.textContent); n.children.forEach(walk); };
rows.forEach(walk);
const control = i => ids['f' + i];

assert.ok(rows.length > 0, 'rows rendered');
assert.ok(labels.filter(Boolean).length >= rows.length, 'labels rendered');

if (kind === 'outbox') {
  // Defaults on first paint.
  assert.deepStrictEqual([0, 1, 2, 3].map(i => control(i).checked), [true, true, true, false]);
  assert.strictEqual(control(4).value, '90');
  assert.strictEqual(control(4).disabled, true, 'age is disabled while auto-delete is off');
  // Toggle auto-delete: the age select unlocks, a full valid config is saved.
  control(3).checked = true; control(3).fire('change');
  assert.strictEqual(control(4).disabled, false);
  assert.deepStrictEqual(saves[saves.length - 1], {
    keep_copies: true, journal_orca_events: true, profile_hint: true, auto_delete: true, auto_delete_days: 90, keep_last: 0 });
  control(4).value = '30'; control(4).fire('change');
  assert.strictEqual(saves[saves.length - 1].auto_delete_days, 30);
  control(0).checked = false; control(0).fire('change');
  assert.strictEqual(saves[saves.length - 1].keep_copies, false);
  // Junk coming from the host falls back to defaults in the form.
  pushConfig({ keep_copies: 'yes', auto_delete_days: 7, auto_delete: 1, extra: true });
  assert.strictEqual(control(0).checked, true);
  assert.strictEqual(control(3).checked, false);
  assert.strictEqual(control(4).value, '90');
  assert.ok(!('clear_history_token' in saves[saves.length - 1]));
} else {
  assert.strictEqual(control(0).checked, true);
  assert.strictEqual(control(1).value, '15');
  assert.strictEqual(control(2).checked, true);
  control(1).value = '60'; control(1).fire('change');
  assert.deepStrictEqual(saves[saves.length - 1], { keep_history: true, poll_seconds: 60, pause_hidden: true });
  control(2).checked = false; control(2).fire('change');
  assert.strictEqual(saves[saves.length - 1].pause_hidden, false);
  control(0).checked = false; control(0).fire('change');
  assert.strictEqual(saves[saves.length - 1].keep_history, false);
  // Clear history: confirmation first, nothing is saved until confirmed.
  const clear = ids.clear, box = ids.confirm;
  assert.ok(clear && box && box.hidden === true);
  const before = saves.length;
  clear.fire('click');
  assert.strictEqual(box.hidden, false);
  assert.strictEqual(saves.length, before, 'no save before confirmation');
  const [okBtn, cancelBtn] = box.children[1].children;
  assert.strictEqual(active, cancelBtn, 'focus starts on Cancel');
  // Escape and Cancel both cancel without saving a token.
  box.fire('keydown', { key: 'Escape' });
  assert.strictEqual(box.hidden, true);
  assert.strictEqual(active, clear, 'focus returns to the button');
  clear.fire('click'); cancelBtn.fire('click');
  assert.strictEqual(box.hidden, true);
  assert.strictEqual(saves.length, before, 'cancel saves nothing');
  // Confirm: settings plus a one-time token are saved.
  clear.fire('click'); okBtn.fire('click');
  const last = saves[saves.length - 1];
  assert.strictEqual(saves.length, before + 1);
  assert.ok(/^[A-Za-z0-9_.:-]{1,80}$/.test(last.clear_history_token), 'token shape');
  assert.strictEqual(last.poll_seconds, 60);
  assert.ok(ids.note.textContent.length > 0, 'status note shown');
  const first = last.clear_history_token;
  // The stored token is preserved by later setting changes, and a second
  // confirmation produces a different token.
  pushConfig({ keep_history: true, poll_seconds: 60, pause_hidden: true, clear_history_token: first });
  control(2).checked = true; control(2).fire('change');
  assert.strictEqual(saves[saves.length - 1].clear_history_token, first);
  clear.fire('click'); okBtn.fire('click');
  assert.notStrictEqual(saves[saves.length - 1].clear_history_token, first);
}
console.log('ok ' + kind);
