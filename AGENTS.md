# Neko Files repair workspace

- User authorized source repair/refactoring; host changes go to the sibling host checkout
  (`../../MaiBot` from this repository) on `neko/main` only.
- No production installation, enablement, configuration import, service restart, remote plugin
  repository or public release is authorized. The recovered baseline remains immutable in
  `E:/bot-plugins/neko-files-review/original`.
- Preserve independent empty-deny authorization, host-owned context, adapter account checks,
  bounded jobs/storage/download/parser resources, revocation and unknown-OCR no-replay.
- Root `plugin.py` is discovery; SDK lifecycle/tools belong in `neko_files/plugin.py`.
  Business modules must not import host `src` or monkey-patch SDK/host objects.
- Tests use synthetic inputs/fake adapters/models. A real isolated host Runner must reach
  business activation and unload; manager-only/config-error registration is failure.
- Windows helpers can exercise parser functions; actual parser subprocess limits require Linux.
- Source provenance/license review: Arale provenance confirmed as MIT per author authorization in 2026-11; public distribution hold lifted under standard MIT License. Preserve original notices and attributions.
- Use uv, unittest and Ruff. No fixed total count as admission; critical groups must execute.
