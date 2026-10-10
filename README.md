# Codex 1M Context

[English](README.md) · [简体中文](README.zh-CN.md) · [हिन्दी](README.hi.md) · [Русский](README.ru.md)

Configure **1,000,000 tokens in the local Codex client** for every model entry in its cached catalog. One offline Python script, no dependencies or network/API calls. It does not read `auth.json` or upload files.

**A 1M display is not a guarantee of 1M input capacity on the server.** This changes client metadata, not the model, your plan, or usage quotas. The provider may reject a request that exceeds its actual limit. Input, instructions, tool data, output, and reasoning share the context window; 1M is not always available entirely for your input.

## Get started

1. Install **Python 3.11 or newer** if needed; the script does not install Python. Use a Codex version that supports `model_catalog_json`.
2. Open the installed Codex once so it creates `models_cache.json` in its Codex home, then close Codex before making changes.
3. Download the repository at https://github.com/IveSan83/codex-1m-context using **Code → Download ZIP**, extract it, and open a terminal in the extracted folder. Alternatively:

```sh
git clone https://github.com/IveSan83/codex-1m-context.git
cd codex-1m-context
```

4. Run the command for your system. On Windows, you can also double-click `START.cmd` to apply the settings; Python 3.11+ is still required.

| Action | Windows | macOS / Linux |
| --- | --- | --- |
| Apply | `py -3 enable_1m.py` | `python3 enable_1m.py` |
| Preview without writing | `py -3 enable_1m.py --dry-run` | `python3 enable_1m.py --dry-run` |
| Restore the latest backup | `py -3 enable_1m.py --restore latest` | `python3 enable_1m.py --restore latest` |

5. Restart Codex. In VS Code, run **Developer: Reload Window** from the Command Palette, then open a new Codex conversation. Check the indicator and behavior yourself.

The default home is `CODEX_HOME` when set, otherwise `~/.codex`. Append `--home "path/to/codex-home"` to any command to select another home; use the same home when restoring.

## What changes

The script reads your local `models_cache.json`, creates its own JSON catalog in your Codex home, and sets every catalog entry's context and maximum context to **1,000,000**, effective context to **100%**, and automatic compaction threshold to **900,000 tokens**. Other model metadata is preserved. At the threshold, Codex may compact conversation history to make space; this is not a guarantee that the server accepts 900,000 tokens.

Only these three root keys in `config.toml` are changed:

- `model_catalog_json`: the generated local catalog.
- `model_context_window = 1000000`
- `model_auto_compact_token_limit = 900000`

Other configuration, including MCP settings, is preserved. Generated catalogs and backups stay in `<Codex home>/codex-1m-context/`. Changes use backups and atomic file writes; repeating the same setup does not create unnecessary new changes. Restore changes only these three keys and preserves later edits to other settings. It refuses conflicts in these keys or modified catalog/backup files. Keep backups private: the original configuration may contain tokens or other secrets.

## Updates and troubleshooting

The generated catalog is a **snapshot**: Codex uses it instead of automatically refreshing the model catalog. After a Codex update, model availability change, or account switch, restore first, open Codex to refresh its normal cache, close it, and run the script again.
Restore returns the previous values. If these already selected another custom catalog, disable that earlier override first to allow the normal cache to refresh.

If `models_cache.json` is missing, open Codex normally once and check that the script selects the same Codex home. A profile or higher-priority setting may override the root values; this tool does not rewrite those overrides. On server context errors, restore the original settings and use the provider's supported limit.

Official OpenAI documentation: https://learn.chatgpt.com/docs/config-file/config-reference and https://developers.openai.com/api/docs/guides/conversation-state

Author: **IveSan83** — https://github.com/IveSan83 · License: [MIT](LICENSE)
