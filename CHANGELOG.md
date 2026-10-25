# 0.2.1 local repair

- Register normal in-message `qq_file_attachments` file metadata alongside group-upload notices; chat-native video attachments (`resource_type=video`) are left to Neko Video, and private-chat file URL retrieval stays unimplemented.
- Registration is bounded (≤8 per message), skips entries without usable identity, and stays scope/account-checked through the existing receive authorization path.
- No production activation, private-attachment retrieval, public plugin release or license relabeling.

# 0.2.0 local repair

- Add explicit configuration version and opt-in trusted host context in the MaiBot fork.
- Separate SDK declarations, scoped workflows, OCR and IO/child-process ownership.
- Atomically publish complete downloads and require source commit/hash records before reuse.
- Short accounting lock, per-file reservations/pins; preserve quotas, revocation and OCR no-replay.
- Store XLSX sheet/row independently; retain bounded parser/archive/continuation behavior.
- Rebuild focused regressions, isolated SDK source packaging and real Runner business activation/RPC/reload/unload QA.
- No production activation, new chat-video ingestion, public plugin release or license relabeling.
