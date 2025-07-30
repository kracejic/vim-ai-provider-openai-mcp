from collections.abc import Sequence, Mapping, Iterator
from typing import Any, Dict, List, Optional, Mapping
import urllib.request
import os
import sys
import json
import vim
from contextlib import AsyncExitStack
import os
import sys
import asyncio
import datetime
import threading
from concurrent.futures import Future
import contextlib

# When importing MCP fails, mcp python library is missing
MCP_import_check = True
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except:
    MCP_import_check = False

MCP_py_imported = True

#------------------------------------------------------------------------------
# Types for vim-ai
class AITextContent(TypedDict):
    type: Literal['text']
    text: str

class AIImageUrlContent(TypedDict):
    type: Literal['image_url']
    image_url: dict[str, str]  # {'url': str}

AIMessageContent = Union[AITextContent, AIImageUrlContent]

class AIMessage(TypedDict):
    role: Literal['system', 'user', 'assistant']
    content: List[AIMessageContent]

class AIUtils(Protocol):
    def print_debug(self, text: str, *args: Any):
        pass
    def make_known_error(self, message: str):
        pass
    def load_api_key(self, env_variable: str, token_file_path: str = "", token_load_fn: str = ""):
        pass

class AIResponseChunk(TypedDict):
    type: Literal['assistant', 'thinking']
    content: str

class AIImageResponseChunk(TypedDict):
    b64_data: str

AICommandType = Literal['chat', 'edit', 'complete', 'image']

class AIProvider(Protocol):
    def __init__(self, command_type: AICommandType, raw_options: Mapping[str, str], utils: AIUtils) -> None:
        pass

    def request(self, messages: Sequence[AIMessage]) -> Iterator[AIResponseChunk]:
        pass

    def request_image(self, prompt: str) -> list[AIImageResponseChunk]:
        pass

#------------------------------------------------------------------------------
# plugin
class OpenAIMCPProvider():
    def __init__(self, command_type: AICommandType, raw_options: Mapping[str, str], utils: AIUtils) -> None:
        self.utils = utils
        self.command_type = command_type
        raw_default_options = vim.eval(f"g:vim_ai_openai_{command_type}")
        self.options = self._parse_raw_options({**raw_default_options, **raw_options})
        self._load_api_key()

    def _protocol_type_check(self) -> None:
        # dummy method, just to ensure type safety
        utils: AIUtils
        options: Mapping[str, str] = {}
        provider: AIProvider = OpenAIProvider('chat', options, utils)

    def tool_call(self, mcps, tool_call):
        # execute the call
        tool_name = tool_call["function"]["name"]
        tool_args = tool_call["function"]["arguments"]
        tool_args = json.loads(tool_args) if tool_args else {}
        self.utils.print_debug(f"Processing tool_call {tool_name} {tool_args}")

        try:
            result = mcps.call(tool_name, tool_args)
            return {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": tool_name,
                "content": [{"type":"text", "text":result.content[0].text}]
            }
        except Exception as e:
            return {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "name": tool_name,
                "content": f"ERROR: {e}"
            }


    def tools_needing_auth(self, tool_calls):
        tools = set([t["function"]["name"] for t in tool_calls])
        self.utils.print_debug(f"USING TOOLS: {tools}")

        if "mcp_dontask" in self.options:
            dontask = set([word.strip() for word in self.options["mcp_dontask"].split(',')])

            if "*" in dontask or ".*" in dontask:
                # anything is allowed
                return set()
            elif len(tools - dontask) == 0:
                # if all tools are in dontask, then allowed
                return set()
            else:
                return tools - dontask

        if "mcp_ask" in self.options:
            ask = set([word.strip() for word in self.options["mcp_ask"].split(',')])
            if len(ask & tools) == 0:
                return set()
            else:
                return ask & tools

        return tools


    def request(self, messages: Sequence[AIMessage]) -> Iterator[AIResponseChunk]:
        mcps = SyncMCPs()
        tools = []
        if "mcp" in self.options:
            self.utils.print_debug(f"Loading mcp: {self.options["mcp"]}")
            mcps.load_cfg(self.options["mcp"])
            tools = mcps.get_tools()
            self.utils.print_debug(f"TOOLS: {tools}")

        # AI LOOP
        shouldContinue = True
        first = True
        while shouldContinue is True:
            # Either process tool_calls
            if "tool_calls" in messages[-1]:
                no_autorun = self.tools_needing_auth(messages[-1]["tool_calls"])
                if first == False and len(no_autorun) > 0:
                    yield {"type":"info", "content":f"Tools that require user confirmation: {no_autorun}, run :AIChat again to authorize"}
                    break
                for tool_call in messages[-1]["tool_calls"]:
                    result = self.tool_call(mcps, tool_call)
                    messages.append(result)
                    yield {"type":"tool_response", "content":json.dumps(result), "newsegment":True}
            else:
                # Or send request to AI
                toolcalls = []
                newMsg = {'role': 'assistant', 'content': [{ 'type': 'text', 'text': '' }]}
                for chunk in self._request(copy.deepcopy(messages), tools):
                    if "tool_calls" not in chunk:
                        # normal response without tool call, append to messages, then yield
                        newMsg["content"][0]["text"] += chunk["content"]
                        yield chunk
                    else:
                        chunk["tool_calls"]
                        if len(chunk["tool_calls"]) > 0 and "index" in chunk["tool_calls"][0]:
                            for tc in chunk["tool_calls"]:
                                i = tc["index"]
                                if len(toolcalls) <= i:
                                    toolcalls.append(tc)
                                else:
                                    toolcalls[i]["function"]["arguments"] \
                                        += tc["function"]["arguments"]
                        else:
                            toolcalls = chunk["tool_calls"]

                if chunk["type"] == "tool_call":
                    res = { "role": "assistant", "content": [{ 'type': 'text', 'text': '' }],
                        "tool_calls": toolcalls }
                    newMsg = res
                    yield {"type":"tool_call", "content":json.dumps(res)}
                else:
                    shouldContinue = False
                messages.append(newMsg)
            first = False

        mcps.cleanup()

    #------------------------------------------------------------------------------
    # Rest is from the upstream
    def _request(self, messages: Sequence[AIMessage], tools=[]) -> Iterator[AIResponseChunk]:
        options = self.options
        openai_options = self._make_openai_options(options)
        http_options = {
            'request_timeout': options['request_timeout'],
            'auth_type': options['auth_type'],
            'token_file_path': options['token_file_path'],
            'token_load_fn': options['token_load_fn'],
        }

        def _flatten_content(messages):
            # NOTE: Some providers like api.deepseek.com & api.groq.com expect a flat 'content' field.
            for message in messages:
                match message['role']:
                    case 'system' | 'assistant':
                        message['content'] = '\n'.join(map(lambda c: c['text'], message['content']))
            return messages

        request = {
            'messages': _flatten_content(messages),
            'tools': tools,
            **openai_options
        }
        self.utils.print_debug("openai: [{}] request: {}", self.command_type, json.dumps(request))
        url = options['endpoint_url']
        response = self._openai_request(url, request, http_options)

        _choice_key = 'delta' if openai_options['stream'] else 'message'

        def _get_delta(resp):
            choices = resp.get('choices') or [{}]
            return choices[0].get(_choice_key, {})

        def _map_chunk(resp):
            self.utils.print_debug("openai: [{}] response: {}", self.command_type, resp)
            delta = _get_delta(resp)
            if delta.get('reasoning_content'):
                # NOTE: support for deepseek's reasoning_content
                return {'type': 'thinking', 'content': delta.get('reasoning_content')}
            if delta.get('reasoning'):
                # NOTE: support for `reasoning` from openrouter
                return {'type': 'thinking', 'content': delta.get('reasoning')}
            if delta.get('tool_calls'):
                return {'type': 'tool_call', 'content':"", 'tool_calls': delta.get('tool_calls')}
            if delta.get('content'):
                return {'type': 'assistant', 'content': delta.get('content')}
            return None # invalid chunk, this occured in deepseek models

        def _filter_valid_chunks(chunk):
            return chunk is not None

        return filter(_filter_valid_chunks, map(_map_chunk, response))

    def _load_api_key(self):
        raw_api_key = self.utils.load_api_key(
            "OPENAI_API_KEY",
            token_file_path=self.options['token_file_path'],
            token_load_fn=self.options['token_load_fn'],
        )
        # The text is in format of "<api key>,<org id>" and the
        # <org id> part is optional
        elements = raw_api_key.strip().split(",")
        api_key = elements[0].strip()
        org_id = None

        if len(elements) > 1:
            org_id = elements[1].strip()

        self.api_key = api_key
        self.org_id = org_id
        return (api_key, org_id)

    def _parse_raw_options(self, raw_options: Mapping[str, Any]):
        if raw_options.get('enable_auth', 1) == "0":
            # raise error for users who don't use default value of this obsolete option
            raise self.utils.make_known_error("`enable_auth = 0` option is no longer supported. use `auth_type = none` instead")

        options = {**raw_options}
        options['request_timeout'] = float(options['request_timeout'])
        if self.command_type != 'image':
            options['max_tokens'] = int(options['max_tokens'])
            options['max_completion_tokens'] = int(options['max_completion_tokens'])
            options['temperature'] = float(options['temperature'])
            options['stream'] = bool(int(options['stream']))
        return options

    def _make_openai_options(self, options):
        max_tokens = options['max_tokens']
        max_completion_tokens = options['max_completion_tokens']
        result = {
            'model': options['model'],
            'stream': options['stream'],
        }
        if options['temperature'] > -1:
            result['temperature'] = options['temperature']

        if 'web_search_options' in options:
            result['web_search_options'] = options['web_search_options']

        if max_tokens > 0:
            result['max_tokens'] = max_tokens
        if max_completion_tokens > 0:
            result['max_completion_tokens'] = max_completion_tokens
        return result

    def request_image(self, prompt: str) -> list[AIImageResponseChunk]:
        options = self.options
        http_options = {
            'request_timeout': options['request_timeout'],
            'auth_type': options['auth_type'],
            'token_file_path': options['token_file_path'],
            'token_load_fn': options['token_load_fn'],
        }
        openai_options = {
            'model': options['model'],
            'quality': options['quality'],
            'size': options['size'],
            'style': options['style'],
            'response_format': 'b64_json',
        }
        request = { 'prompt': prompt, **openai_options }
        self.utils.print_debug("openai: [{}] request: {}", self.command_type, request)
        url = options['endpoint_url']
        response, *_ = self._openai_request(url, request, http_options)
        self.utils.print_debug("openai: [{}] response: {}", self.command_type, { 'images_count': len(response['data']) })
        b64_data = response['data'][0]['b64_json']
        return [{ 'b64_data': b64_data }]

    def _openai_request(self, url, data, options):
        RESP_DATA_PREFIX = 'data: '
        RESP_DONE = '[DONE]'

        auth_type = options['auth_type']
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "VimAI",
        }

        if auth_type == 'bearer':
            headers['Authorization'] = f"Bearer {self.api_key}"

            if self.org_id is not None:
                headers["OpenAI-Organization"] =  f"{self.org_id}"

        if auth_type == 'api-key':
            headers['api-key'] = f"{self.api_key}"

        request_timeout=options['request_timeout']
        req = urllib.request.Request(
            url,
            data=json.dumps({ **data }).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=request_timeout) as response:
            if not data.get('stream', 0):
                yield json.loads(response.read().decode())
                return
            for line_bytes in response:
                line = line_bytes.decode("utf-8", errors="replace")
                if line.startswith(RESP_DATA_PREFIX):
                    line_data = line[len(RESP_DATA_PREFIX):-1]
                    if line_data.strip() == RESP_DONE:
                        pass
                    else:
                        openai_obj = json.loads(line_data)
                        yield openai_obj

#------------------------------------------------------------------------------
# Upstream ends
#------------------------------------------------------------------------------
# MCP stuff
# from typing import Any, Dict, List, Optional, Mapping
# from contextlib import AsyncExitStack
# import os
# import sys
# import asyncio
# import datetime
# import json
# import threading
# from concurrent.futures import Future
# import contextlib
# # When importing MCP fails, mcp python library is missing
# MCP_import_check = True
# try:
#     from mcp import ClientSession, StdioServerParameters
#     from mcp.client.stdio import stdio_client
# except:
#     MCP_import_check = False
#
# MCP_py_imported = True

class MCPs(object):
    def __init__(self):
        self.mcps: Dict[str, MCP] = {}
        self.tool_to_mcp: Dict[str, str] = {}
        self.done_event = asyncio.Event()

    async def done(self):
        self.done_event.set()

    async def init(self):
        self._exit_stack = AsyncExitStack()

    async def init_loop_clean(self):
        await self.init()
        await self.done_event.wait()
        await self.cleanup()

    async def add_tool(self, name: str, tool: Dict) -> bool:
        mpc = MCP(self._exit_stack)
        if "type" in tool and tool["type"] == "sse":
            #TODO
            pass
        else:
            await mpc.init_stdio(tool)
        self.mcps[name] = mpc
        # Map tool names to their MCP instances
        if hasattr(mpc, 'tools') and mpc.tools:
            for tool_info in mpc.tools:
                if 'function' in tool_info and 'name' in tool_info['function']:
                    self.tool_to_mcp[tool_info['function']['name']] = name
        return True

    async def load_cfg(self, path: str) -> bool:
        try:
            path = os.path.expandvars(os.path.expanduser(path))
            with open(path, 'r') as f:
                cfg = json.load(f)
                for key, tool in cfg.items():
                    print_debug(f"MCP loading tool {key}")
                    await self.add_tool(key, tool)
            return True
        except (FileNotFoundError, json.JSONDecodeError):
            print_debug(f"Error: MCP could not open the file {path}")
            return False

    def get_tools(self) -> List:
        ret = []
        for key, mcp in self.mcps.items():
            ret.extend(mcp.tools)
            # print(f"{key} => {mcp.tools}")
        return ret

    async def call(self, tool: str, args) -> str:
        if tool in self.tool_to_mcp:
            return await self.mcps[self.tool_to_mcp[tool]].call(tool, args)
        else:
            raise Exception(f"{tool} not found in MCPs")

    async def cleanup(self):
        if self._exit_stack is not None:
            await self._exit_stack.__aexit__(None, None, None)
            self._exit_stack = None


#------------------------------------------------------------------------------
class MCP(object):
    def __init__(self, exit_stack):
        self.session: Optional[ClientSession] = None
        self.read_stream: Optional[Any] = None
        self.write_stream: Optional[Any] = None
        self.tools: List[Dict[str, Any]] = []
        self._exit_stack = exit_stack

    async def init_stdio(self, server_config: Dict) -> List[str]:
        # without this we get annoying messages messing up with vim rendering
        # also neovim does not work.
        devnull = open("/dev/null", "w")

        server_params = StdioServerParameters(**server_config)

        # 2) Enter stdio_client into the stack
        self.read_stream, self.write_stream = await self._exit_stack.enter_async_context(
            stdio_client(server_params, errlog=devnull)
        )

        # 3) Enter ClientSession into the same stack
        self.session = await self._exit_stack.enter_async_context(
            ClientSession(self.read_stream, self.write_stream)
        )

        # Now the session is alive until .cleanup()
        await self.session.initialize()
        mcp_tools = await self.session.list_tools()
        self.tools = [self.convert_tool_format(t) for t in mcp_tools.tools]

        return [t["function"]["name"] for t in self.tools]

    async def call(self, tool: str, args) -> str:
        if self.session is None:
            raise RuntimeError("MCP not initialized")
        return await self.session.call_tool(tool, args)

    def convert_tool_format(self, tool):
        converted_tool = {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": {
                    "type": "object",
                    "properties": tool.inputSchema["properties"],
                    "required": tool.inputSchema["required"]
                }
            }
        }
        return converted_tool


#------------------------------------------------------------------------------
class SyncMCPs:
    def __init__(self):
        # Create the real async object
        self._mcp = MCPs()

        # Spin up an event‐loop in its own thread
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._start_loop, daemon=True)
        self._thread.start()

        # Call its async init() once at startup, do not wait
        asyncio.run_coroutine_threadsafe(self._mcp.init_loop_clean(), self._loop)

    def _start_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def load_cfg(self, path: str) -> bool:
        if MCP_import_check == False:
            raise Exception("Python library MCP import has failed, you can't use MCP.")
        return asyncio.run_coroutine_threadsafe(self._mcp.load_cfg(path), self._loop).result()

    def call(self, tool: str, args) -> str:
        # early return when no MCP library
        if MCP_import_check == False:
            return ""
        return asyncio.run_coroutine_threadsafe(self._mcp.call(tool, args), self._loop).result()

    def get_tools(self):
        return self._mcp.get_tools()

    def cleanup(self):
        # early return when no MCP library
        if MCP_import_check == False:
            return
        # trigger the async cleanup
        asyncio.run_coroutine_threadsafe(self._mcp.done(), self._loop).result()

        # stop the loop, join the thread
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()

        # close the loop
        self._loop.close()

