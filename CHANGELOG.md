# 0.3.3 SnowLuma original group file IDs

- Accept only slash-prefixed canonical hex UUIDs in verified group catalog/notice/message sources; keep path/private-scope refusal and existing authorization.
- Preserve opaque IDs and old references without substitution; no local-path reading or get_file/search fallback.

# 0.3.2 SnowLuma private-file compatibility

- Preserve bounded structured attachment file_hash privately and pass it to private URL resolution when present.
- Revalidate a hash-missing original private message once, without replacing its file identity; reject changed hashes and preserve TTL/cache/OCR attempts.
- No local-path/get_file fallback, new configuration or permissions, OCR calls, or changes to the public-distribution hold.

# 0.2.3 local repair

- Tool hints: move the operation-critical semantics out of `detailed_description` (which this host never sends to the model) into the model-visible briefs and parameter descriptions — pagination starts at 1, OCR is explicit single-page and admin-gated, cursors are for continuation, archive members must match the inspected listing.
- No behavior change; registration/download paths stay as in 0.2.2.

# 0.2.2 local repair

- Fix the 0.2.1 registration assumption: this host's `additional_config` never carries `qq_file_attachments` (that structure belongs to a different environment). In-message files are instead recovered from the adapter's flattened `[文件] name，大小: n，链接: <url>` text marker; the direct link is stored as the item identity and used verbatim on download, no adapter call needed to fetch a URL.
- Group-upload notice registration is unchanged. Downloads still pass the public-only URL validator with full commit records; unsupported extensions and size limits still refuse before any network work.
- Message-link registrations report size 0 until first download (the marker carries no reliable size); the per-file limit is enforced against the real downloaded length.
- No production activation, private-attachment API retrieval, public plugin release or license relabeling.

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
# 0.3.1 source preparation

- Separate group catalog metadata sizes from download limits: an unrelated large file no longer rejects the whole directory.
- Preserve usable rows with explicit skipped/partial indicators; never report all-invalid metadata as an empty directory.
- Clarify root/real folder IDs and incomplete searches; identify failed QQ action/retcode without raw response disclosure.
- No production deployment, config/permission changes, QQ download/OCR or license-hold removal.
