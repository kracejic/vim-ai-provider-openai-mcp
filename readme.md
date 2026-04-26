# vim-ai provider with MCP support

🚨 **Announcement** 🚨

This is an early version that requires **heavy**-testing at the moment.

## Description

This is a provider for [vim-ai](https://github.com/madox2/vim-ai).

## Installation

This plugin requires

* Vim with `python3` support.

* [mcp](https://github.com/modelcontextprotocol/python-sdk) library from Anthropic.

    ```bash
    pip install mcp --user
    ```

* Binaries/Services

    Also, as a precursory step, any of the mcp application-runners (like
    `uv` for `uvx`, `nodejs`/`npm` for `npx`, or `docker`) and their
    dependencies need to be setup on the machine as required by the
    configuration.

Then we can add this into vim with bundle, requires `vim-ai` to be
installed before:

```vim
Plug 'madox2/vim-ai'
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
options.mcp=~/.vim/mcp.json
options.mcp_dontask=list_directory,directory_tree
options.mcp_secrets=~/.vim/mcp-secrets.json
options.mcp_state=~/.vim/mcp-state.json

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

Path to a MCP configuration file (e.g. `~/.vim/mcp.json`).

Servers are defined as key-value pairs under the `mcpServers` key. There
are a few special provisions like

* Variable replacement

    * Environment variables are replaced.

    * Adds `UID` and `GID` variables to denote the user-id and group-id
      of the current user, respectively.

    * Variables read from the file `mcp-secrets.json` are replaced.

    * Supports using `$varname` / `${varname}`, or
      `${varname:-defaultvalue}` to interpolate environment variables in
      the JSON configuration.

* Optional `meta` field that is unused, to add description, comments,
  relevant links or notes, etc.

* Additional `prompt` field, servers can define their custom system and
  user prompts in this field and those get aggregated and injected into
  the request prompts. Can be a string or list, for the latter, strings
  starting with a `!` (bang) are skipped.

* Servers that have either `enabled: false` or `disabled: true` defined
  are skipped from loading.

* For servers that require docker to run,

    * The variables defined in the `env` field are inserted after the
      `run` argument. No need for "-e", "VAR=val" in the `args` field.

    * Variables defined in `env` that start with a `!` (bang) are
      skipped from being added into the command arguments.

    * Default behaviour runs the servers in stateless mode, i.e.
      containers are stopped (or removed forcefully) once the tool call
      is complete. This can be disabled by setting `stateful: true` in
      the configuration. However, that may or may not work since the
      session and streams will be closed.

#### options.mcp_secrets

Optional path to a JSON file (e.g. `~/.vim/mcp-secrets.json`) containing
key-value pairs that are merged with `os.environ` before substituting
those keys with their values in the configurations defined in
`mcp.json`.

#### options.mcp_state

Optional path to a JSON file (e.g.  `~/.vim/mcp-state.json`) used to
cache the retrieved tools along with their function-mappings. This way,
once the cache is populated, we can skip the first call to `tool_list`
(that happens by default to fetch the list of tools from all configured
servers) and only run the relevant server when the function is called
from LLM responses.

However, there is currently **no way to manage this file through vim**
right now, so, until that gets fixed,

* To refresh any tool, simply locate the same-named key-value pair under
  the key `tools` in the cache file, and delete it. It will get
  refetched before the next query. (E.g. for `files` the key is `files`
  and the values should be the list of tools fetched from the server)

* To fully clear the cache, **you have to delete the file manually**,
  (optionally put it in `/tmp` to clean up during reboot).

* currently the file (if exists) is checked for freshness and refetched
  when stale, by default every 12 hours.

#### options.mcp_ask

A comma-separated list of tools for which the plugin should ask for confirmation. By default, this is not specified, and the plugin asks for confirmation for all tools except the ones specified in next option.

> If this option is specified, following option is not considered.

#### options.mcp_dontask

A comma-separated list of tools for which the plugin should not ask for confirmation and call the tool right away.

> Special values "\*" or ".\*" means ANY.

## Examples

### Vim-AI configuration

```vim
let g:vim_ai_chat = #{
\  provider: "openai-mcp",
\  prompt: "",
\  options: #{
\    endpoint_url: g:vim_ai_endpoint_url,
\    initial_prompt: s:initial_chat_prompt,
\    auth_type: "none",
\    request_timeout: 20,
\    selection_boundary: "#####",
\    stream: 1,
\    token_file_path: "",
\    mcp: "~/.vim/mcp.json",
\    mcp_dontask: "*",
\    mcp_secrets: "~/.vim/mcp-secrets.json",
\    mcp_state: "~/.vim/mcp-state.json",
\  },
\  ui: #{
\    code_syntax_enabled: 1,
\    populate_options: 0,
\    populate_all_options: 0,
\    paste_mode: 1,
\  },
\}
```

### MCP configuration file

At the moment only stdio | streamable_http is tested/supported.

```json
{
  "mcpServers": {
    "filesystem": {
      "enabled": false,
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "./"]
    },
    "fetch": {
      "command": "uvx",
      "args": ["mcp-server-fetch"]
    },
    "chrome-devtools": {
      "meta": {
        "description": "(dockerized) Browser MCP Server to control local Chromium instance",
        "link": "https://github.com/ChromeDevTools/chrome-devtools-mcp"
      },
      "disabled": false,
      "stateful": false,
      "type": "stdio",
      "command": "docker",
      "args": [
        "run",
        "--cpu-shares=256",
        "--entrypoint=npx",
        "--interactive",
        "--memory=512mb",
        "--network=host",
        "--rm",
        "--user=$UID:$GID",
        "node:alpine",
        "-y",
        "chrome-devtools-mcp@latest",
        "--browserUrl=http://127.0.0.1:9222",
        "--no-usage-statistics"
      ],
      "env": {
        "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "true",
        "CHROME_DEVTOOLS_MCP_NO_USAGE_STATISTICS": "true"
      },
      "prompt": {
        "system": [
          "You have the ability to control your own Chromium Browser instance.",
          "Based on user request, always call your own browser tools first.",
          "But if the tool-call fails, guide the user through the manual process.",
          "Always remember the browser tab that is currently opened, or call the tools to know."
        ]
      }
    },
    "files": {
      "meta": {
        "description": "(dockerized) Filesystem Tools at `/project`",
        "link": "https://github.com/modelcontextprotocol/servers/tree/main/src/files"
      },
      "disabled": false,
      "stateful": false,
      "type": "stdio",
      "command": "docker",
      "args": [
        "run",
        "--cpu-shares=256",
        "--interactive",
        "--memory=512mb",
        "--mount",
        "type=bind,src=${PWD:-/path/to/your/project},dst=/project",
        "--name=vimcp_files",
        "--rm",
        "--user=$UID:$GID",
        "mcp/filesystem",
        "/project"
      ],
      "prompt": {
        "system": [
          "You are able to modify the filesystem using tool-calls.",
          "Unless explicitly specified by user, set the current working directory to '/project'."
        ]
      }
    },
    "git": {
      "meta": {
        "description": "(dockerized) Git Tools at `/project`",
        "link": "https://github.com/cyanheads/git-mcp-server"
      },
      "disabled": false,
      "stateful": false,
      "type": "stdio",
      "command": "docker",
      "args": [
        "run",
        "--cpu-shares=512",
        "--entrypoint=npx",
        "--interactive",
        "--memory=1536mb",
        "--mount",
        "type=bind,src=${PWD:-/path/to/your/project},dst=/project",
        "--rm",
        "--user=$UID:$GID",
        "node:alpine",
        "-y",
        "@cyanheads/git-mcp-server@latest"
      ],
      "env": {
        "PGID": "$GID",
        "PUID": "$UID",
        "S6_VERBOSITY": "0",
        "!GIT_BASE_DIR": "/project",
        "!GIT_EMAIL": "user@host.local",
        "!GIT_SIGN_COMMITS": "false",
        "!GIT_USERNAME": "user",
        "!LOGS_DIR": "/tmp/logs",
        "!MCP_LOG_LEVEL": "info",
        "!MCP_RESPONSE_VERBOSITY": "standard",
        "!MCP_SESSION_MODE": "stateless",
        "!MCP_TRANSPORT_TYPE": "stdio",
        "!OTEL_ENABLED": "false",
        "!STORAGE_PROVIDER_TYPE": "in-memory"
      },
      "prompt": {
        "system": [
          "You have the ability to use the 'git' tool expertly to version-control, check or detect differences in code.",
          "Unless explicitly specified by user, set the git working directory to '/project'.",
          "!This system-prompt is not inserted because it starts with a (!) bang."

        ],
        "user": [
          "You prefer reading raw formatted diffs, and output markdown-formatted diff-messages to user.",
          "!This user-prompt is not inserted because it starts with a (!) bang."
        ]
      }
    },
    "mcpjungle": {
      "meta": {
        "description": "(remote-proxy) Collection of Tools at MCPJungle for the Client: ${MCPJUNGLE_CLIENT}",
        "link": "https://github.com/mcpjungle/MCPJungle"
      },
      "disabled": false,
      "type": "http",
      "url": "${MCPJUNGLE_URL}",
      "headers": {
        "Authorization": "Bearer ${MCPJUNGLE_TOKEN}",
        "X-MCPJ-Client": "${MCPJUNGLE_CLIENT}"
      }
    },
    "memory": {
      "meta": {
        "description": "(dockerized) Filesystem Tools at `/project`",
        "link": "https://github.com/modelcontextprotocol/servers/tree/main/src/memory"
      },
      "disabled": false,
      "stateful": false,
      "type": "stdio",
      "command": "docker",
      "args": [
        "run",
        "--cpu-shares=256",
        "--interactive",
        "--memory=512mb",
        "--rm",
        "--mount",
        "type=bind,src=${PWD:-/path/to/your/project},dst=/project",
        "--user=$UID:$GID",
        "mcp/memory"
      ],
      "env": {
        "MEMORY_FILE_PATH": "/project/vimcp-memory.jsonl"
      },
      "prompt": {
        "system": [
          "You can store data in a persistent knowledge graph via tool-calling that you refer to as your 'memory'.",
          "You can create and manipulate observations, entities, relationships, graphs and nodes.",
          "Follow these steps (and call the relevant tools) for each interaction:",
          "",
          "1. User Identification:",
          "   - You should assume that you are interacting with default_user",
          "   - If you have not identified default_user, proactively try to do so.",
          "",
          "2. Memory Retrieval:",
          "   - Always begin your chat by saying only 'Remembering...' and call your tools to retrieve all relevant information from your persistent knowledge graph",
          "   - Always refer to your persistent knowledge graph as your 'memory'",
          "",
          "3. Memory",
          "   - While conversing with the user, be attentive to any new information that falls into these categories:",
          "     a) Basic Identity (age, gender, location, job title, education level, etc.)",
          "     b) Behaviors (interests, habits, etc.)",
          "     c) Preferences (communication style, preferred language, etc.)",
          "     d) Goals (goals, targets, aspirations, etc.)",
          "     e) Relationships (personal and professional relationships up to 3 degrees of separation)",
          "",
          "4. Memory Update:",
          "   - If any new information was gathered during the interaction, update your persistent knowledge graph as follows:",
          "     a) Create entities for recurring organizations, people, and significant events",
          "     b) Connect them to the current entities using relations",
          "     c) Store facts about them as observations"
        ]
      }
    }
  }
}
```

### MCP Secrets file

```json
{
  "GIT_EMAIL": "user@host",
  "GIT_USERNAME": "user",
  "MCPJUNGLE_CLIENT": "developer",
  "MCPJUNGLE_TOKEN": "<token-of-developer>",
  "MCPJUNGLE_URL": "http://<mcpjungle.service.local>:<port>/mcp"
}
```
