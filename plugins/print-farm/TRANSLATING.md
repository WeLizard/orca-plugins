# Translating Print Farm

Print Farm follows `orca.host.app_language()` and accepts every locale currently
published by OrcaSlicer:

`ca`, `cs`, `de`, `en`, `es`, `eu`, `fr`, `hu`, `it`, `ja`, `ko`, `lt`, `nl`,
`pl`, `pt_BR`, `ru`, `sv`, `th`, `tr`, `uk`, `vi`, `zh_CN`, `zh_TW`.

Translations live in `printers_locales/<locale>.json`. A complete catalog is
bundled for every locale above; any future missing key still falls back to
English. Print Farm owns this namespace independently from the FilamentHub
catalog even though both plugins use the same validation contract. Only
user-facing Print Farm copy belongs here: printer protocols, commands, and raw
vendor diagnostics remain in their original wire format.

To contribute:

1. Copy `printers_locales/en.json` to the required OrcaSlicer locale name, or
   edit an existing catalog.
2. Translate values only. Do not rename keys or placeholders such as `{name}`,
   `{count}`, `{printer}` and `{error}`.
3. Keep the file UTF-8 JSON and do not add runtime code or remote resources.
4. Run:

   ```powershell
   python validate_locales.py
   python -m unittest -q
   ```

Translation-only pull requests are welcome. Translator credit is recorded in
the release notes when the catalog is bundled into a wheel.
