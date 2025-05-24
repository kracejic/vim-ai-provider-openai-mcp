# vim-ai provider with MCP support

🚨 **Announcement** 🚨

This is an early version that support only stdio at the moment. It also works only with a fork of vim-ai.

## Description

This is a provider for [vim-ai](https://github.com/madox2/vim-ai), but at the moment it only works with [my fork](https://github.com/kracejic/vim-ai/) untill we manage to push needed changes upstream.

This plugin requires mcp library from Anthropic.

```bash
pip install mcp --user
```

You can add this into vim with bundle:

```vim
Plug 'kracejic/vim-ai'
Plug 'kracejic/vim-ai-provider-openai-mcp'
```


## Example chat

```
[chat]
provider=openai-mcp
options.temperature=1
options.request_timeout=300
options.model=gpt-4.1-mini
options.max_completion_tokens=10000
options.stream=1
options.mcp=~/bin/mcp/dev.json
options.mcp_dontask=list_directory,directory_tree

>>> user
 
What files are in this folder? And also tell me a short summary of plugin/vim-ai-provider-openai-mcp.vim file? Do it in one call. 

<<< tool_call

{"role": "assistant", "content": [{"type": "text", "text": ""}], "tool_calls": [{"index": 0, "id": "call_uz0PShYFT9bbqs4h7wA44B2T", "type": "function", "function": {"name": "list_directory", "arguments": "{\"path\": \"plugin\"}"}}, {"index": 1, "id": "call_zxQORr1SdGb1pNSW9fR3XcJh", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"plugin/vim-ai-provider-openai-mcp.vim\"}"}}]}

<<< info

Tools that require user confirmation: {'read_file'}, run :AIChat again to authorize

<<< tool_response

{"role": "tool", "tool_call_id": "call_uz0PShYFT9bbqs4h7wA44B2T", "name": "list_directory", "content": [{"type": "text", "text": "[FILE] vim-ai-provider-openai-mcp.vim"}]}

<<< tool_response

{"role": "tool", "tool_call_id": "call_zxQORr1SdGb1pNSW9fR3XcJh", "name": "read_file", "content": [{"type": "text", "text": "let s:plugin_root = expand('<sfile>:p:h:h')\n\n\" Register the provider\ncal vim_ai_provider#Register('openai-mcp', {\n\\  'script_path': s:plugin_root . '/py/openai_mcp.py',\n\\  'class_name': 'OpenAIMCPProvider',\n\\})\n"}]}

<<< assistant

The folder "plugin" contains one file:
- vim-ai-provider-openai-mcp.vim

A short summary of the file "vim-ai-provider-openai-mcp.vim":
This Vim script file registers a provider named 'openai-mcp' for the Vim AI plugin. It sets the root directory of the plugin and registers the provider with a script path pointing to a Python file 'openai_mcp.py' located in the 'py' directory relative to the plugin root. The provider class used is named 'OpenAIMCPProvider'.
```

## Configuration

### Provider configuration

#### options.mcp

Path to a MCP configuration file.

#### options.mcp_ask

A comma-separated list of tools for which the plugin should ask for confirmation. By default, this is not specified, and the plugin asks for confirmation for all tools except the ones specified in next option.

> If this option is specified, following option is not considered.

#### options.mcp_dontask

A comma-separated list of tools for which the plugin should not ask for confirmation and call the tool right away.

> Special values "*" or ".*" means ANY.


### Example MCP configuration file

At the moment only stdio is supported.

```json
{
    "filesystem": {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "./"]
    },
    "fetch": {
        "command": "uvx",
        "args": ["mcp-server-fetch"]
    }
}
```

