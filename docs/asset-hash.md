# Asset content hash

Every asset version carries `content_hash`, so two versions (or a version and what a runner wrote to disk) are
equal exactly when their hashes are. The implementation is `app/asset_hash.py`.

    hash = sha256_hex( utf8( compact_json( {type, schema_version, key, fields, body, files} ) ) )

- `files` is a list of `[path, sha256]` pairs sorted by path; `fields` is the JSON object of the version.
- JSON: object keys sorted (by code point), separators `,` and `:` with no spaces, non-ASCII characters written as
  themselves (not `\uXXXX`).
- Every string, object keys included, is NFC-normalised, and CR LF and lone CR become LF.
- Floats are not allowed in `fields`.
- Saving content whose hash equals the current version's is a no-op.
