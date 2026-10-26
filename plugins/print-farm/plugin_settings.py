"""Print Farm settings: defaults, validation and the Plugins-dialog page.

Stdlib only. The host stores each capability's config as open JSON, so nothing
here may hold a secret. Anything unknown, mistyped or out of range falls back
to the default; extra keys are ignored.
"""

import json
import re

OUTBOX = "outbox"
DASHBOARD = "dashboard"

POLL_CHOICES = (5, 15, 30, 60)
AUTO_DELETE_CHOICES = (30, 90, 180, 365)
KEEP_LAST_CHOICES = (0, 3, 5, 10, 20)  # 0 = keep all

_DEFAULTS = {
    OUTBOX: {
        "keep_copies": True,
        "journal_orca_events": True,
        "profile_hint": True,
        "auto_delete": False,
        "auto_delete_days": 90,
        "keep_last": 0,
    },
    DASHBOARD: {
        "keep_history": True,
        "poll_seconds": 15,
        "pause_hidden": True,
    },
}
_CHOICES = {
    "poll_seconds": POLL_CHOICES,
    "auto_delete_days": AUTO_DELETE_CHOICES,
    "keep_last": KEEP_LAST_CHOICES,
}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]{1,80}")


def defaults(kind):
    """Explicit default config (what "Restore defaults" writes)."""
    return dict(_DEFAULTS.get(kind, {}))


def _parse(raw):
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def normalize(kind, raw):
    """Valid settings for `kind` from any stored value (JSON text, dict, junk)."""
    result = defaults(kind)
    data = _parse(raw)
    for key, default in result.items():
        value = data.get(key, default)
        if isinstance(default, bool):
            if isinstance(value, bool):
                result[key] = value
        elif isinstance(value, int) and not isinstance(value, bool) \
                and value in _CHOICES[key]:
            result[key] = value
    if kind == DASHBOARD:
        token = data.get("clear_history_token")
        result["clear_history_token"] = (
            token if isinstance(token, str) and _TOKEN_RE.fullmatch(token) else "")
    return result


def fields(kind, text):
    """Page field specs; `text(key, **values)` supplies localized labels."""
    def check(key, label, hint=""):
        return {"key": key, "type": "bool", "label": text(label), "hint": text(hint) if hint else ""}

    if kind == OUTBOX:
        return [
            check("keep_copies", "cfgKeepCopies", "cfgKeepCopiesHint"),
            check("journal_orca_events", "cfgJournalOrca", "cfgJournalOrcaHint"),
            check("profile_hint", "cfgProfileHint", "cfgProfileHintHint"),
            check("auto_delete", "cfgAutoDelete", "cfgAutoDeleteHint"),
            {"key": "auto_delete_days", "type": "select", "label": text("cfgAutoDeleteAge"),
             "hint": "", "requires": "auto_delete",
             "options": [[d, text("cfgDays", days=d)] for d in AUTO_DELETE_CHOICES]},
            {"key": "keep_last", "type": "select", "label": text("cfgKeepLast"),
             "hint": text("cfgKeepLastHint"),
             "options": [[n, text("cfgKeepAll") if n == 0 else str(n)] for n in KEEP_LAST_CHOICES]},
        ]
    if kind == DASHBOARD:
        return [
            check("keep_history", "cfgKeepHistory", "cfgKeepHistoryHint"),
            {"key": "poll_seconds", "type": "select", "label": text("cfgPollInterval"),
             "hint": "", "options": [[s, text("cfgSeconds", seconds=s)] for s in POLL_CHOICES]},
            check("pause_hidden", "cfgPauseHidden", "cfgPauseHiddenHint"),
        ]
    return []


_PAGE = r"""<!DOCTYPE html>
<html lang="__LANG__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin:0; padding:14px 16px; background:var(--orca-bg,#1e1e2e); color:var(--orca-fg,#e0e0e0);
         font-family:var(--orca-font,sans-serif); font-size:13px; line-height:1.4; }
  .row { padding:9px 0; border-bottom:1px solid var(--orca-border,#3c3c4c); }
  .row:last-of-type { border-bottom:0; }
  .row.sub { padding-left:26px; }
  label { display:flex; align-items:flex-start; gap:9px; cursor:pointer; }
  label.sel { cursor:default; flex-wrap:wrap; align-items:center; }
  input[type=checkbox] { margin:2px 0 0; width:15px; height:15px; accent-color:var(--orca-accent,#009688); flex:0 0 auto; }
  select { font:inherit; color:var(--orca-fg,#e0e0e0); background:var(--orca-bg,#1e1e2e);
           border:1px solid var(--orca-border,#3c3c4c); border-radius:5px; padding:4px 8px; min-height:28px; }
  select:disabled, input:disabled + span { opacity:.5; }
  .hint { margin:2px 0 0 24px; font-size:11px; color:var(--orca-muted,#a0a0a0); }
  .row.sub .hint, label.sel + .hint { margin-left:0; }
  button { font:inherit; padding:5px 12px; border-radius:5px; cursor:pointer;
           background:transparent; color:var(--orca-fg,#e0e0e0); border:1px solid var(--orca-border,#3c3c4c); }
  button.danger { background:var(--orca-accent,#009688); color:var(--orca-accent-fg,#fff); border-color:var(--orca-accent,#009688); }
  :focus-visible { outline:2px solid var(--orca-accent,#009688); outline-offset:2px; }
  #confirm { margin-top:8px; padding:10px; border:1px solid var(--orca-border,#3c3c4c); border-radius:6px; }
  #confirm .btns { display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; }
  #note { margin-top:6px; font-size:11px; color:var(--orca-muted,#a0a0a0); }
</style></head><body>
<div id="form"></div>
<script>
(function(){
  var spec = __SPEC__;
  var current = {};
  var controls = {};
  var form = document.getElementById('form');

  function normalized(raw){
    raw = (raw && typeof raw === 'object') ? raw : {};
    var out = {};
    spec.fields.forEach(function(f){
      var def = spec.defaults[f.key], v = raw[f.key];
      if (f.type === 'bool') out[f.key] = (typeof v === 'boolean') ? v : def;
      else {
        var ok = typeof v === 'number' && f.options.some(function(o){ return o[0] === v; });
        out[f.key] = ok ? v : def;
      }
    });
    return out;
  }
  function el(tag, attrs, text){
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function(k){ node.setAttribute(k, attrs[k]); });
    if (text) node.textContent = text;
    return node;
  }
  function values(){
    var out = {};
    spec.fields.forEach(function(f){
      var c = controls[f.key];
      out[f.key] = f.type === 'bool' ? c.checked : Number(c.value);
    });
    return out;
  }
  function save(extra){
    var out = values();
    if (spec.clear && current.clear_history_token) out.clear_history_token = current.clear_history_token;
    if (extra) Object.keys(extra).forEach(function(k){ out[k] = extra[k]; });
    if (window.orca && window.orca.saveConfig) window.orca.saveConfig(out);
  }
  function syncEnabled(){
    spec.fields.forEach(function(f){
      if (f.requires) controls[f.key].disabled = !controls[f.requires].checked;
    });
  }
  spec.fields.forEach(function(f, i){
    var row = el('div', {'class': 'row' + (f.requires ? ' sub' : '')});
    var hintId = 'hint' + i, c;
    if (f.type === 'bool') {
      var lab = el('label');
      c = el('input', {type: 'checkbox', id: 'f' + i});
      lab.appendChild(c); lab.appendChild(el('span', {}, f.label));
      row.appendChild(lab);
    } else {
      var l2 = el('label', {'class': 'sel', 'for': 'f' + i});
      l2.appendChild(el('span', {}, f.label));
      c = el('select', {id: 'f' + i});
      f.options.forEach(function(o){ c.appendChild(el('option', {value: String(o[0])}, o[1])); });
      l2.appendChild(c); row.appendChild(l2);
    }
    if (f.hint) { row.appendChild(el('div', {'class': 'hint', id: hintId}, f.hint)); c.setAttribute('aria-describedby', hintId); }
    c.addEventListener('change', function(){ syncEnabled(); save(); });
    controls[f.key] = c;
    form.appendChild(row);
  });

  var clearBtn = null;
  if (spec.clear) {
    var row = el('div', {'class': 'row'});
    clearBtn = el('button', {type: 'button', id: 'clear'}, spec.clear.label);
    row.appendChild(clearBtn);
    var box = el('div', {id: 'confirm', role: 'alertdialog', 'aria-labelledby': 'confirm-text'});
    box.hidden = true;
    box.appendChild(el('div', {id: 'confirm-text'}, spec.clear.confirm));
    var btns = el('div', {'class': 'btns'});
    var ok = el('button', {type: 'button', 'class': 'danger'}, spec.clear.label);
    var cancel = el('button', {type: 'button'}, spec.clear.cancel);
    btns.appendChild(ok); btns.appendChild(cancel); box.appendChild(btns);
    row.appendChild(box);
    var note = el('div', {id: 'note', role: 'status', 'aria-live': 'polite'});
    row.appendChild(note);
    form.appendChild(row);
    var closeBox = function(focusBack){
      box.hidden = true; clearBtn.disabled = false;
      if (focusBack) clearBtn.focus();
    };
    clearBtn.addEventListener('click', function(){
      box.hidden = false; clearBtn.disabled = true; note.textContent = ''; cancel.focus();
    });
    cancel.addEventListener('click', function(){ closeBox(true); });
    box.addEventListener('keydown', function(e){
      if (e.key === 'Escape') { e.preventDefault(); closeBox(true); }
      else if (e.key === 'Tab') {
        var first = cancel, last = ok;
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    });
    ok.addEventListener('click', function(){
      var token = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
        : (Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12));
      closeBox(true);
      save({clear_history_token: token});
      note.textContent = spec.clear.done;
    });
  }

  function apply(raw){
    current = normalized(raw);
    if (spec.clear) current.clear_history_token =
      (raw && typeof raw.clear_history_token === 'string') ? raw.clear_history_token : '';
    spec.fields.forEach(function(f){
      var c = controls[f.key];
      if (f.type === 'bool') c.checked = !!current[f.key]; else c.value = String(current[f.key]);
    });
    syncEnabled();
  }
  if (window.orca && window.orca.onConfig) window.orca.onConfig(apply);
  else apply(window.orca && window.orca.getConfig ? window.orca.getConfig() : {});
})();
</script></body></html>
"""


def build_page(kind, text, language="en", clear=None):
    """HTML for the capability's settings tab, labels from `text(key, **values)`."""
    spec = {"kind": kind, "defaults": defaults(kind), "fields": fields(kind, text)}
    if clear:
        spec["clear"] = clear
    payload = json.dumps(spec, ensure_ascii=False).replace("<", "\\u003c")
    lang = re.sub(r"[^A-Za-z0-9-]", "", str(language or "en").replace("_", "-")) or "en"
    return _PAGE.replace("__LANG__", lang).replace("__SPEC__", payload)
