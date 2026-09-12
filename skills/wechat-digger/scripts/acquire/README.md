# acquire/ — WeChat decrypted-vault read helpers

This directory ships **read-only helpers only**. The key-extraction and
SQLCipher decryption stack is intentionally **not distributed** with the
public package — decrypt with a tool of your choice first, then point
wechat-digger at the plaintext vault.

| Script | Role |
|--------|------|
| `vault_cli.py` | read-only query on decrypted vault (status/sessions/contacts/history/moments/favorites/…) |
| `export_chat.py` | export one chat full/incremental (needs `zstandard`) |

Removed from the internal build, on purpose: `extract_keys.py` (key
capture/match), `decrypt_all_dbs.py` (decrypt → private vault),
`list_contacts.py` / `search_sns.py` (legacy helpers with built-in AES
decryption). Corresponding `wd.py` subcommands (`keys` / `decrypt` /
`refresh`) report the stack as missing — by design.

Config/data stay outside the skill tree:

- `~/.config/wechat-local-vault.json` — vault location config (optional)
- `~/Library/Application Support/wechat-local-vault/` — default plaintext vault dir, or set `WECHAT_PRIVATE_VAULT`

Prefer calling via `wd.py vault|export-msg|…` so path resolution is unified.
