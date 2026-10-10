# Codex 1M Context

[English](README.md) · [简体中文](README.zh-CN.md) · [हिन्दी](README.hi.md) · [Русский](README.ru.md)

将本地缓存模型目录中每个条目的 **Codex 客户端上下文设置为 1,000,000 个 token**。只需一个离线 Python 脚本，无第三方依赖，不发起网络或 API 请求，不读取 `auth.json`，也不上传文件。

**显示 1M 不代表服务器一定接受 1M 个输入 token。** 此工具修改的是客户端元数据，不会改变模型能力、套餐或使用配额。请求超出实际限制时，服务提供方仍可能拒绝。输入、指令、工具数据、输出和推理都会占用上下文窗口，因此不能总把全部 1M 用于自己的输入。

## 开始使用

1. 如有需要，先安装 **Python 3.11 或更高版本**；脚本不会自动安装 Python。Codex 版本需要支持 `model_catalog_json`。
2. 先打开已安装的 Codex 一次，让它在 Codex 主目录中生成 `models_cache.json`，然后关闭 Codex 再修改配置。
3. 在 https://github.com/IveSan83/codex-1m-context 点击 **Code → Download ZIP**，解压后在该文件夹中打开终端。也可以使用 Git：

```sh
git clone https://github.com/IveSan83/codex-1m-context.git
cd codex-1m-context
```

4. 运行适合当前系统的命令。Windows 用户也可以双击 `START.cmd` 应用设置；仍需安装 Python 3.11 或更高版本。

| 操作 | Windows | macOS / Linux |
| --- | --- | --- |
| 应用设置 | `py -3 enable_1m.py` | `python3 enable_1m.py` |
| 预览，不写入文件 | `py -3 enable_1m.py --dry-run` | `python3 enable_1m.py --dry-run` |
| 恢复最近的备份 | `py -3 enable_1m.py --restore latest` | `python3 enable_1m.py --restore latest` |

5. 重启 Codex。在 VS Code 的命令面板中运行 **Developer: Reload Window**，再新建 Codex 对话。请自行检查显示和实际行为。

默认使用环境变量 `CODEX_HOME` 指定的目录；未设置时使用 `~/.codex`。在任意命令后添加 `--home "path/to/codex-home"` 可选择其他主目录；恢复时也要使用同一目录。

## 修改内容

脚本读取本地 `models_cache.json`，在 Codex 主目录中生成自己的 JSON 模型目录，并将每个条目的上下文和最大上下文设为 **1,000,000**、有效上下文比例设为 **100%**、自动压缩阈值设为 **900,000 个 token**。其余模型元数据保持原样。达到阈值时，Codex 可能压缩对话历史以腾出空间；这不保证服务器接受 900,000 个 token。

只修改 `config.toml` 根级别的以下三个键：

- `model_catalog_json`：生成的本地模型目录。
- `model_context_window = 1000000`
- `model_auto_compact_token_limit = 900000`

包括 MCP 在内的其他配置会保留。生成的目录文件和备份保存在 `<Codex home>/codex-1m-context/`。修改前创建备份，写入采用原子替换；重复应用相同设置不会产生多余改动。恢复时只还原这三个键，并保留其他设置的后续编辑；如果这三个键存在冲突，或目录文件、备份被修改，则拒绝恢复。请勿公开备份：原始配置可能包含访问令牌或其他秘密信息。

## 更新与排查

生成的目录是一个**快照**：Codex 使用该文件后，不会自动刷新模型目录。更新 Codex、模型可用性发生变化或切换账号后，请先恢复原配置，再打开 Codex 刷新正常缓存，关闭后重新运行脚本。
恢复会还原之前的键值。如果之前已指定其他自定义模型目录，需要先停用原有覆盖设置，正常缓存才能刷新。

如果缺少 `models_cache.json`，请先正常打开 Codex 一次，并确认脚本使用相同的主目录。配置档或更高优先级的设置可能覆盖根级别配置；本工具不会重写这些覆盖项。服务器报上下文超限时，请恢复原设置，并使用服务提供方支持的限制。

OpenAI 官方文档：https://learn.chatgpt.com/docs/config-file/config-reference 和 https://developers.openai.com/api/docs/guides/conversation-state

作者：**IveSan83** — https://github.com/IveSan83 · 许可证：[MIT](LICENSE)
