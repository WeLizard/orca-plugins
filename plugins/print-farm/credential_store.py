"""Explicit operating-system credential providers and immutable bound entries."""
import hashlib
import json
import os
import sys
import threading
import uuid

FIELDS = ("apikey", "access_code")
USERNAME = "Print Farm"

class Unavailable(Exception):
    pass

def system_backend():
    # Never invoke keyring discovery, config, or environment-selected providers.
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring
        class Provider(WinVaultKeyring):
            def set_properties_from_env(self):
                pass
        result = Provider()
        result.persist = "local_machine"
    elif sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
        class Provider(Keyring):
            def set_properties_from_env(self):
                pass
        result = Provider()
        result.keychain = None
    elif sys.platform.startswith("linux"):
        from keyring.backends.SecretService import Keyring
        class Provider(Keyring):
            def set_properties_from_env(self):
                pass
        result = Provider()
    else:
        raise Unavailable()
    return result

def binding(root, card, field):
    parts = [
        os.path.normcase(os.path.realpath(root)), card.get("owner_scope_uuid"),
        card.get("target_uuid"), card.get("url"), card.get("type"),
        card.get("target_selector"), field,
    ]
    if card.get("type") == "repetier":
        parts.append(card.get("repetier_slug"))
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()

def service(root, card, field, ref):
    if not isinstance(ref, dict) or set(ref) != {"id", "binding"}:
        raise Unavailable()
    try:
        identity = str(uuid.UUID(ref["id"]))
    except (ValueError, TypeError, AttributeError):
        raise Unavailable() from None
    digest = binding(root, card, field)
    if ref["binding"] != digest or identity != ref["id"]:
        raise Unavailable()
    return "PrintFarm:" + digest + ":" + identity

class Vault:
    def __init__(self, factory=system_backend):
        self.factory = factory
        self.cache = {}
        self.epoch = 0
        self.lock = threading.RLock()
        self.access_lock = threading.Lock()

    def clear(self):
        with self.lock:
            self.epoch += 1
            self.cache.clear()

    def write(self, root, card, field, value, is_current=lambda: True):
        if (not isinstance(value, str) or not value or
                len(value.encode("utf-8")) > 512):
            raise Unavailable()
        with self.lock:
            epoch = self.epoch
        if not is_current():
            raise Unavailable()
        ref = {"id": str(uuid.uuid4()), "binding": binding(root, card, field)}
        try:
            provider = self.factory()
            name = service(root, card, field, ref)
            if not is_current():
                raise Unavailable()
            provider.set_password(name, USERNAME, value)
            if not is_current() or provider.get_password(name, USERNAME) != value:
                raise Unavailable()
        except Exception:
            raise Unavailable() from None
        with self.lock:
            if epoch != self.epoch or not is_current():
                raise Unavailable()
        return ref

    def read(self, root, card, field, ref, is_current=lambda: True):
        name = service(root, card, field, ref)
        # Provider calls may block on an OS dialog. Lifecycle invalidation must
        # remain nonblocking and must discard the late outcome.
        with self.lock:
            epoch = self.epoch
        with self.access_lock:
            with self.lock:
                if epoch != self.epoch or not is_current():
                    raise Unavailable()
                cached = name in self.cache
                value = self.cache.get(name)
            if not cached:
                try:
                    value = self.factory().get_password(name, USERNAME)
                    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
                        raise Unavailable()
                except Exception:
                    value = None
                with self.lock:
                    if epoch != self.epoch or not is_current():
                        raise Unavailable()
                    self.cache[name] = value
            if value is None or not is_current():
                raise Unavailable()
            return value
