from collections.abc import Sequence, Mapping, Iterator
from typing import Any
from typing import Dict, List, Optional, Set
import urllib.request
import os
import json
import vim
import asyncio

# from concurrent.futures import Future
# import contextlib
from contextlib import AsyncExitStack
from pathlib import Path
import httpx
import re
import sys
import threading
import time

# When importing MCP fails, mcp python library is missing
MCP_import_check = True
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client
except:
    MCP_import_check = False

MCP_py_imported = True

if "VIMAI_DUMMY_IMPORT" in os.environ:
    # TODO: figure out how to properly use imports/modules in vim, dev environment, pytest
    # types.py is a softlink to ../../vim-ai/py/types.py which
    # is expected to exist since vim-ai requires to be installed
    from py.types import (
        AIMessage,
        AIResponseChunk,
        AIUtils,
        AIProvider,
        AICommandType,
        AIImageResponseChunk,
    )

# TODO: promote constants/global vars


class OpenAIMCPProvider(object):
    default_options_varname_chat = "g:vim_ai_openai_chat"
    default_options_varname_complete = "g:vim_ai_openai_complete"
    default_options_varname_edit = "g:vim_ai_openai_edit"
    default_options_varname_image = "g:vim_ai_openai_image"

    def __init__(
        self,
        command_type: AICommandType,
        raw_options: Mapping[str, str],
        utils: AIUtils,
    ) -> None:
        self.utils = utils
        self.command_type = command_type

        config_varname = getattr(self, f"default_options_varname_{command_type}")
        raw_default_options = vim.eval(config_varname)
        self.options = self._parse_raw_options({**raw_default_options, **raw_options})

        self._load_api_key()

        self.smm = SyncMCPManager(self.utils)
        if "mcp" in self.options:
            self.smm.configure(
                cfile=self.options.get("mcp", ""),
                secfile=self.options.get("mcp_secrets", ""),
                sfile=self.options.get("mcp_state", ""),
                fnames_ask_approve=self.options.get("mcp_ask", []),
                fnames_auto_approve=self.options.get("mcp_dontask", []),
            )

    def _protocol_type_check(self) -> None:
        # dummy method, just to ensure type safety
        utils: AIUtils
        options: Mapping[str, str] = {}
        provider: AIProvider = OpenAIProvider("chat", options, utils)

    def request(self, messages: Sequence[AIMessage]) -> Iterator[AIResponseChunk]:
        tools = []
        if "mcp" in self.options:
            tools = self.smm.tool_list_all()
            # self.utils.print_debug(f"{self.__class__.__name__}: INFO:  TOOLS: {tools}")
            # include mcp prompts in messages
            self.smm.mm.inject_prompts(messages)

        # AI LOOP
        shouldContinue = True
        first = True
        while shouldContinue is True:  # process tool_calls in last message
            if ("tool_calls" in messages[-1]) and len(messages[-1]["tool_calls"]):
                needs_approval = self.smm.mm.check_user_approval(
                    messages[-1]["tool_calls"]
                )
                if first == False and len(needs_approval) > 0:
                    yield {
                        "type": "info",
                        "content": f"Tools that require user approval: {needs_approval}, run :AIChat again to authorize",
                    }
                    break
                for tc in messages[-1]["tool_calls"]:
                    result = self.smm.tool_call(tc)
                    messages.append(result)
                    yield {
                        "type": "tool_response",
                        "content": json.dumps(result),
                        "newsegment": True,
                    }
            else:  # send request to AI
                toolcalls = []
                newMsg = {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                }
                for chunk in self._request(copy.deepcopy(messages), tools):
                    if "tool_calls" not in chunk:
                        # normal response without tool call, append to messages, then yield
                        newMsg["content"][0]["text"] += chunk["content"]
                        yield chunk
                    else:
                        chunk["tool_calls"]
                        if (
                            len(chunk["tool_calls"]) > 0
                            and "index" in chunk["tool_calls"][0]
                        ):
                            for tc in chunk["tool_calls"]:
                                i = tc["index"]
                                if len(toolcalls) <= i:
                                    toolcalls.append(tc)
                                else:
                                    toolcalls[i]["function"]["arguments"] += tc[
                                        "function"
                                    ]["arguments"]
                        else:
                            toolcalls = chunk["tool_calls"]

                if chunk["type"] == "tool_call":
                    res = {
                        "role": "assistant",
                        "content": [{"type": "text", "text": ""}],
                        "tool_calls": toolcalls,
                    }
                    newMsg = res
                    yield {"type": "tool_call", "content": json.dumps(res)}
                else:
                    shouldContinue = False
                messages.append(newMsg)
            first = False

        self.smm.cleanup()

    def _request(
        self, messages: Sequence[AIMessage], tools=[]
    ) -> Iterator[AIResponseChunk]:
        options = self.options

        def _make_openai_options(options: Dict[str, any]) -> Dict[str, any]:
            result = {"model": options["model"]}
            option_keys = [
                "stream",
                "temperature",
                "max_tokens",
                "max_completion_tokens",
                "web_search_options",
                "frequency_penalty",
                "logit_bias",
                "logprobs",
                "presence_penalty",
                "reasoning_effort",
                "seed",
                "stop",
                "top_logprobs",
                "top_p",
                "reasoning",  # openrouter reasoning parameter
            ]
            for key in option_keys:
                if key not in options:
                    continue
                value = options[key]
                if value == "":
                    continue
                # Backward compatibility: before using empty string "", values below
                # were used to exclude these params from the request
                if key == "temperature" and value == -1:
                    continue
                if key == "max_tokens" and value == 0:
                    continue
                if key == "max_completion_tokens" and value == 0:
                    continue
                result[key] = value
            return result

        openai_options = _make_openai_options(options)

        http_options = {
            "request_timeout": options.get("request_timeout") or 20,
            "auth_type": options["auth_type"],
            "token_file_path": options["token_file_path"],
            "token_load_fn": options["token_load_fn"],
        }

        def _flatten_content(messages) -> List:
            # NOTE: Some providers like api.deepseek.com & api.groq.com expect a flat 'content' field.
            for message in messages:
                match message["role"]:
                    case "system" | "assistant" | "user":
                        message["content"] = "\n".join(
                            map(lambda c: c["text"], message["content"])
                        )
            return messages

        request = {
            "messages": _flatten_content(messages),
            "tools": tools,
            "tool_choice": "auto",
            **openai_options,
        }
        # self.utils.print_debug("{self.__class__.__name__}: INFO: [{}] request: {}", self.command_type, json.dumps(request, indent=2))
        url = options["endpoint_url"]
        response = self._openai_request(url, request, http_options)

        _choice_key = "delta" if openai_options.get("stream") else "message"

        def _get_delta(resp) -> Dict:
            choices = resp.get("choices") or [{}]
            return choices[0].get(_choice_key, {})

        def _map_chunk(resp):
            # self.utils.print_debug("{self.__class__.__name__}: INFO: [{}] response: {}", self.command_type, resp)
            delta = _get_delta(resp)
            if delta.get("reasoning_content"):
                # NOTE: support for deepseek's reasoning_content
                return {"type": "thinking", "content": delta.get("reasoning_content")}
            if delta.get("reasoning"):
                # NOTE: support for `reasoning` from openrouter
                return {"type": "thinking", "content": delta.get("reasoning")}
            if delta.get("tool_calls"):
                return {
                    "type": "tool_call",
                    "content": "",
                    "tool_calls": delta.get("tool_calls"),
                }
            if delta.get("content"):
                return {"type": "assistant", "content": delta.get("content")}
            return None  # invalid chunk, this occured in deepseek models

        def _filter_valid_chunks(chunk) -> bool:
            return chunk is not None

        return filter(_filter_valid_chunks, map(_map_chunk, response))

    def _load_api_key(self):
        raw_api_key = self.utils.load_api_key(
            "OPENAI_API_KEY",
            token_file_path=self.options["token_file_path"],
            token_load_fn=self.options["token_load_fn"],
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
        if raw_options.get("enable_auth", 1) == "0":
            # raise error for users who don't use default value of this obsolete option
            raise self.utils.make_known_error(
                "`enable_auth = 0` option is no longer supported. use `auth_type = none` instead"
            )

        options = {**raw_options}

        def _convert_option(name, converter):
            if (
                name in options
                and isinstance(options[name], str)
                and options[name] != ""
            ):
                try:
                    options[name] = converter(options[name])
                except (ValueError, TypeError, json.JSONDecodeError) as e:
                    raise self.utils.make_known_error(
                        f"Invalid value for option '{name}': {options[name]}. Error: {e}"
                    )

        _convert_option("request_timeout", float)

        if self.command_type != "image":
            _convert_option("stream", lambda x: bool(int(x)))
            _convert_option("max_tokens", int)
            _convert_option("max_completion_tokens", int)
            _convert_option("temperature", float)
            _convert_option("frequency_penalty", float)
            _convert_option("presence_penalty", float)
            _convert_option("top_p", float)
            _convert_option("seed", int)
            _convert_option("top_logprobs", int)
            _convert_option("logprobs", lambda x: bool(int(x)))
            _convert_option("stop", json.loads)
            _convert_option("logit_bias", json.loads)
            # reasoning_effort is a string, no conversion needed

            # openrouter reasoning parameter: https://openrouter.ai/docs/use-cases/reasoning-tokens#controlling-reasoning-tokens
            _convert_option("reasoning", json.loads)

        return options

    def request_image(self, prompt: str) -> list[AIImageResponseChunk]:
        options = self.options
        http_options = {
            "request_timeout": options["request_timeout"],
            "auth_type": options["auth_type"],
            "token_file_path": options["token_file_path"],
            "token_load_fn": options["token_load_fn"],
        }
        openai_options = {
            "model": options["model"],
            "quality": options["quality"],
            "size": options["size"],
            "style": options["style"],
            "response_format": "b64_json",
        }
        request = {"prompt": prompt, **openai_options}
        self.utils.print_debug(
            "{self.__class__.__name__}: INFO: [{}] request: {}",
            self.command_type,
            request,
        )
        url = options["endpoint_url"]
        response, *_ = self._openai_request(url, request, http_options)
        self.utils.print_debug(
            "{self.__class__.__name__}: INFO: [{}] response: {}",
            self.command_type,
            {"images_count": len(response["data"])},
        )
        b64_data = response["data"][0]["b64_json"]
        return [{"b64_data": b64_data}]

    def _openai_request(self, url, data, options):
        RESP_DATA_PREFIX = "data: "
        RESP_DONE = "[DONE]"

        auth_type = options["auth_type"]
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/6.0 (X11; U; Linux i686) Gecko/20071127 Firefox/2.0.0.11",
            "Accept-Encoding": "gzip, deflate",
            "Accept": "*/*",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
        }

        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {self.api_key}"

            if self.org_id is not None:
                headers["OpenAI-Organization"] = f"{self.org_id}"

        if auth_type == "api-key":
            headers["api-key"] = f"{self.api_key}"

        request_timeout = options["request_timeout"]
        req = urllib.request.Request(
            url,
            data=json.dumps({**data}).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        proxy_settings = self.utils.get_proxy_settings()
        if proxy_settings:
            proxy_handler = urllib.request.ProxyHandler(proxy_settings)
            opener = urllib.request.build_opener(proxy_handler)
            response = opener.open(req, timeout=request_timeout)
        else:
            response = urllib.request.urlopen(req, timeout=request_timeout)

        with response:
            if not data.get("stream", 0):
                yield json.loads(response.read().decode())
                return
            for line_bytes in response:
                line = line_bytes.decode("utf-8", errors="replace")
                if line.startswith(RESP_DATA_PREFIX):
                    line_data = line[len(RESP_DATA_PREFIX) : -1]
                    if line_data.strip() == RESP_DONE:
                        pass
                    else:
                        openai_obj = json.loads(line_data)
                        yield openai_obj


class SyncMCPManager(object):
    """
    Wraps asynchronous MCPManager tasks into synchronous calls
    """

    def __init__(self, utils: AIUtils) -> None:
        """
        Initialize the MCPManager instance.

        This method performs the following steps:
        1. Checks if the MCP library is available. If not, raises an exception.
        2. Stores the provided AIUtils instance for utility access.
        3. Spins up an asyncio event loop in its own daemon thread for asynchronous operations.
        4. Initializes the MCPManager with the provided AIUtils instance.
        5. Asynchronously starts the MCPManager's initialization process without waiting for completion.

        Args:
            utils (AIUtils): An instance of the AIUtils class providing utility functions.

        Raises:
            Exception: If the Python library MCP import fails, indicating the MCP library is not available.

        Note:
            This method starts an asyncio event loop and runs the MCPManager's async `start` method in the background.
            The event loop runs in a daemon thread, ensuring it will terminate when the main program exits.
        """
        if not MCP_import_check:
            raise Exception("Python library MCP import has failed, you can't use MCP.")

        self.utils = utils
        # Spin up an event‐loop in its own thread
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._looper, daemon=True)
        self._thread.start()

        self.mm = MCPManager(self.utils)

        # Call its async init() once at startup, do not wait
        Helpers.run_task(self._loop, self.mm.start)

    def _looper(self) -> None:
        """
        Sets the current event loop to the instance's loop and runs it forever.

        Note:
            This method should only be called once and typically during application
            initialization. It is not intended for frequent or repeated calls.

        Warning:
            The loop will run forever unless manually stopped (e.g., via
            `self._loop.stop()` or by setting up a signal handler to interrupt it).

        Raises:
            RuntimeError: If the event loop is already running or if the loop is
                        not properly initialized.
        """
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def configure(
        self,
        cfile: Path,
        secfile: Path = "",
        sfile: Path = "",
        fnames_ask_approve: List[str] = [],
        fnames_auto_approve: List[str] = [],
    ) -> bool:
        """
        Configures the system by invoking the `configure` method of the `self.mm` (MCPManager) object with the provided parameters.
        If the MCP import check fails, the method returns an empty string instead of proceeding with the configuration.

        Parameters:
        -----------
        cfile : Path
            The main configuration file path.
        secfile : Path, optional
            The secondary configuration file path. Defaults to an empty string.
        sfile : Path, optional
            The third configuration file path. Defaults to an empty string.
        fnames_ask_approve : List[str], optional
            List of filenames that require user approval before proceeding. Defaults to an empty list.
        fnames_auto_approve : List[str], optional
            List of filenames that are automatically approved without user interaction. Defaults to an empty list.

        Returns:
        --------
        bool
            Returns True if configuration succeeds. Returns False if MCP library is not available.
            In case of MCP import failure, returns an empty string (intended to be a dict, but not implemented).

        Notes:
        ------
        - This function relies on the `Helpers.run_afun` method to execute the configuration asynchronously.
        - Ensure that the MCP library is properly imported before calling this function.
        - If `cfile` is not provided, the function will fail silently.
        """
        if not MCP_import_check:  # early return when no MCP library
            return ""  # should be dict
        return Helpers.run_afun(
            self._loop,
            self.mm.configure,
            cfile=cfile,
            secfile=secfile,
            sfile=sfile,
            fnames_ask_approve=fnames_ask_approve,
            fnames_auto_approve=fnames_auto_approve,
        )

    def tool_call(self, tc: Dict[str, any]) -> Dict[str, any]:
        """
        Orchestrates the execution of a asynchronous tool call in a synchronous way.

        This method checks whether the MCP library is available. If it is not, the method returns an empty string to indicate failure or absence of functionality. Otherwise, it delegates the actual tool execution to the `Helpers.run_afun` function, which runs the specified tool asynchronously using the provided coroutine (`self.mm.tool_call`) and tool call parameters (`tc`).

        Parameters:
            tc (Dict[str, any]): A dictionary containing the parameters and context for the tool call. This typically includes the tool name, arguments, and other metadata necessary for executing the tool.

        Returns:
            Dict[str, any]: The result of the tool call as returned by `Helpers.run_afun`. If the MCP library is not available, returns an empty string.

        Note:
            - This method is designed to be used within an synchronous context.
            - If `MCP_import_check` is `False`, no further processing occurs, and an empty string is returned immediately.
            - The `Helpers.run_afun` function is responsible for handling the actual asynchronous execution and managing any potential errors or results from the tool call.
        """
        if not MCP_import_check:  # early return when no MCP library
            return ""  # should be dict
        return Helpers.run_afun(self._loop, self.mm.tool_call, tc)

    def tool_list_all(self, refresh: bool = False) -> List:
        """
        Retrieves a list of all available tools in the current configuration.

        This function checks if the MCP library is available. If not, it returns an empty list.
        Otherwise, it asynchronously calls the `MCPManager.tool_list_all` method using the `Helpers.run_afun` class,
        passing the `refresh` flag to determine whether to refresh the tool list.

        Args:
            refresh (bool, optional): If True, forces a refresh of the tool list. Defaults to False.

        Returns:
            List: A list of available tools. Returns an empty list if the MCP library is not available.
        """
        if not MCP_import_check:  # early return when no MCP library
            return []
        return Helpers.run_afun(self._loop, self.mm.tool_list_all, refresh)

    def cleanup(self) -> None:
        """
        Perform cleanup operations for the current instance, ensuring proper shutdown of asynchronous resources.

        This method is responsible for gracefully shutting down the event loop and associated threads.
        It performs the following actions:
        1. Returns early if the MCP library is not available (MCP_import_check is False).
        2. Triggers async task-completion via `self.mm.finish`.
        3. Identifies any pending or cancelled tasks (not yet completed) and waits for them to finish.
        4. Stops the event loop using `loop.stop()`.
        5. Joins the associated thread to ensure it has finished execution.
        6. Closes the event loop to free resources.

        This method should be called when shutting down the instance to ensure no lingering asynchronous operations remain.
        """
        if not MCP_import_check:  # early return when no MCP library
            return
        Helpers.run_afun(self._loop, self.mm.finish)  # trigger async cleanup

        # give any pending or cancelled tasks
        # the last chance to run and complete
        tasks = [
            ta
            for ta in asyncio.all_tasks(loop=self._loop)
            if not (ta.done() or ta.cancelled())
        ]
        if len(tasks):
            Helpers.run_afun(self._loop, Helpers.wait_for_tasks, tasks)

        # calling loop.stop() unblocks run_forever()
        self._loop.call_soon_threadsafe(self._loop.stop)  # stop the loop
        self._thread.join()  # join the thread
        self._loop.close()  # close the loop


class MCPManager(object):
    """
    Manages MCPClient(s), asynchronously-by-default.
    """

    _exit_stack: Optional[AsyncExitStack] = None
    fname_map_tool: Mapping[str, str] = {}
    # cfile: Path = ""
    # # "~/.vim/mcp.json" # not needed
    sfile: Path = ""
    # # "/tmp/vim-ai-state.json"|"~/.vim/vim-ai-state.json"

    def __init__(self, utils: AIUtils) -> None:
        """
        Initialize the class with an AIUtils instance and set up internal state.

        This method initializes the instance with the provided AIUtils object, which is used
        for utility operations. It also initializes a dictionary to store MCPClient instances
        by their identifiers and creates an asyncio.Event object to signal when operations
        are finished.

        Args:
            utils (AIUtils): An instance of the AIUtils class providing utility methods.

        Attributes:
            utils (AIUtils): The provided AIUtils instance for utility operations.
            clients (Dict[str, MCPClient]): Dictionary mapping client identifiers to MCPClient instances.
            ev_task_done (asyncio.Event): An asyncio.Event used to signal completion of operations.
        """
        self.utils = utils

        self.clients: Dict[str, MCPClient] = {}

        # signals we acquired AsyncExitStack, MCP configurations can begin
        self.ev_init_done = asyncio.Event()
        self.timeout_init_done: int = 1  # seconds

        # signals tasks complete, exit (gracefully if possible) and cleanup after
        self.ev_task_done = asyncio.Event()
        self.timeout_task_done: int = 20  # seconds

    async def finish(self) -> None:
        """
        Marks the current operation or process as finished by signaling the completion event.

        This method sets the `ev_task_done` event, which is typically used to notify waiting
        coroutines or threads that the task has completed. It is commonly used in asynchronous
        contexts to coordinate between tasks or to signal the end of a phase in a larger process.

        This is a no-op if `ev_task_done` is not a valid event object or if it has already been set.

        Raises:
            AttributeError: If `ev_task_done` is not set or is not an event object.
            RuntimeError: If the event is already set (and re-setting is not supported).
        """
        self.ev_task_done.set()

    async def init(self) -> None:
        """
        Initializes the instance by creating an AsyncExitStack to manage asynchronous resources.
        This ensures that resources can be properly cleaned up when the instance is closed or when an error occurs.

        The AsyncExitStack provides a way to manage a series of async context managers, automatically handling cleanup even in the presence of exceptions.

        Attributes:
            self._exit_stack (AsyncExitStack): An instance of AsyncExitStack to manage async resources.

        Returns:
            None
        """
        self._exit_stack = AsyncExitStack()
        self.ev_init_done.set()

    async def start(self) -> None:
        """
        Initiates the main execution flow of the component.

        This method orchestrates the startup sequence by:
        1. Calling `init()` to perform any necessary initialization.
        2. Waiting for the `ev_task_done` event to be set, which signals that the component is ready to proceed or has been instructed to stop.
        3. Executing `cleanup()` to gracefully shut down and release resources.

        This is typically called when the component is first activated or resumed after a pause.

        Raises:
            Any exceptions raised by `init()`, `ev_task_done.wait()`, or `cleanup()` will propagate up the call stack.

        Note:
            This method is designed to be awaited, making it suitable for use in async contexts.
            It should not be called concurrently unless explicitly designed to handle it.
        """
        await self.init()
        await asyncio.wait_for(self.ev_task_done.wait(), self.timeout_task_done)
        await self.cleanup()

    async def configure(
        self,
        cfile: Path,
        secfile: Path = "",
        sfile: Path = "",
        fnames_ask_approve: List[str] = [],
        fnames_auto_approve: List[str] = [],
    ) -> bool:
        """
        Configures the MCP server by loading configuration and secret files,
        and initializing MCP clients based on the provided configuration.

        This method reads the main configuration file (cfile) and optionally a secrets file (secfile),
        merging them using environment variable substitution. It then iterates over the 'mcpServers'
        section in the configuration, creating and initializing MCP clients for each enabled server.

        Parameters:
            cfile (Path): The path to the main configuration file in JSON format.
            secfile (Path, optional): The path to the secrets file in JSON format. Defaults to an empty path.
            sfile (Path): The path to the state cache file
            fnames_ask_approve : List[str]
                List of function names that require explicit approval. If any function in
                `tool_calls` matches a name in this list, it will be included in the
                return set. If empty, no function are required to be approved via this
                mechanism.
            fnames_auto_approve : List[str]
                List of function names that are explicitly allowed without approval.
                If any function in `tool_calls` matches a name in this list, it will be
                excluded from the return set. If empty, all functions are subject to
                approval unless otherwise specified.

        Returns:
            bool: Always returns True to indicate successful configuration (even if no clients are enabled).

        Notes:
            - The method skips server configurations where 'disabled' is True or 'enabled' is False.
            - The actual client creation is delegated to the MCPClient class constructor.
            - Environment variable substitution is applied using the Helpers.envsubst function.
            - The 'sfile' parameter is set here, used to store state.
        """
        # self.cfile = cfile
        # self.secfile = secfile
        self.sfile = sfile
        self.fnames_ask_approve = fnames_ask_approve
        self.fnames_auto_approve = fnames_auto_approve

        # self.utils.print_debug(
        #     f"{self.__class__.__name__}: INFO: Loading mcps from: {cfile}"
        # )
        config = Helpers.envsubst(
            Helpers.json_read(cfile, bydef={}),
            secrets=Helpers.json_read(secfile, bydef={}),
        )

        # delay parsing to ensure we obtained AsyncExitStack before
        # individual MCP configurations start trying to put stuff in it
        await asyncio.wait_for(self.ev_init_done.wait(), self.timeout_init_done)

        def _isToolEnabled(conf: Dict[str, any]) -> bool:
            # skipped if either is set
            #   "disabled": true
            #   "enabled":  false
            # TODO: selective loading based on filetype / project ??
            return (not bool(conf.get("disabled", 0))) and bool(conf.get("enabled", 1))

        # servers under key 'mcpServers'
        for name, conf in config.get("mcpServers", {}).items():
            if _isToolEnabled(conf):
                # self.utils.print_debug(f"{self.__class__.__name__}: INFO: loading tool {len(self.clients.values())+1} {name}")
                # self.utils.print_debug(f"{self.__class__.__name__}: {conf}")
                self.clients[name] = MCPClient(self._exit_stack, name, conf, self.utils)
        return True

    def inject_prompts(self, messages: Sequence[AIMessage]) -> None:
        """
        Injects system and user prompts into the message sequence by modifying the content of the first system or user message respectively.

        This method first collects all unique system and user prompts configured across all MCP clients using `_prompts_list_all()`.
        It then modifies the message sequence by appending system prompts after the first system message (if any) and prepending user prompts before the first user message (if any).
        Each prompt is injected as a text content item within the message's content list.

        Args:
            messages (Sequence[AIMessage]): A sequence of AI messages to which prompts will be injected.

        Returns:
            None
        """

        def _prompts_list_all() -> Dict[str, List[str]]:
            """
            Collects and returns all unique system and user prompts configured across all MCP clients.

            This method iterates through each MCP client in the `self.clients` dictionary and retrieves
            the configured system and user prompts from their respective configuration dictionaries.
            If the prompt values are lists, they are joined into a single string with newline separators.
            Duplicate prompts are filtered out to ensure each prompt appears only once in the result.

            Returns:
                Dict[str, List[str]]: A dictionary with two keys:
                    - "system": A list of unique system prompts.
                    - "user": A list of unique user prompts.

            Example:
                {
                    "system": ["You are a helpful assistant.", "Follow instructions carefully."],
                    "user": ["Please provide the answer.", "Do not hallucinate."]
                }
            """
            prompts = {"system": [], "user": []}
            for name, mcp in self.clients.items():
                sysp = mcp.prompt.get("system", "")
                # stringify lists and skip lines starting with a (!) bang
                if isinstance(sysp, list):
                    sysp = "\n".join([p for p in sysp if not p.startswith("!")])
                if sysp and (sysp not in prompts["system"]):  # skip duplicates
                    prompts["system"].append(sysp)

                usrp = mcp.prompt.get("user", "")
                # stringify lists and skip lines starting with a (!) bang
                if isinstance(usrp, list):
                    usrp = "\n".join([p for p in usrp if not p.startswith("!")])
                if usrp and (usrp not in prompts["user"]):  # skip duplicates
                    prompts["user"].append(usrp)
            # self.utils.print_debug(
            #     f"{self.__class__.__name__}: INFO:  Prompts: {prompts}"
            # )
            return prompts

        # modify system/user prompts
        # TODO: also check upstream and fetch preset tool prompts if any
        prompts = _prompts_list_all()
        if prompts.get("system", []):
            # locate the content of first system message (assuming atleast one exists)
            for m in messages:
                if m.get("role", "") == "system":
                    # append the prompts as text after
                    # TODO: check duplicate
                    for sysp in prompts.get("system", []):
                        m.get("content", []).append({"type": "text", "text": sysp})
                    break
        if prompts.get("user", []):
            # locate the content of first user message (assuming atleast one exists)
            for m in messages:
                if m.get("role", "") == "user":
                    # prepend the prompts as text before its own prompts
                    # TODO: check duplicate
                    for usrp in prompts.get("user", []):
                        m.get("content", []).insert(0, {"type": "text", "text": usrp})
                    break
        # self.utils.print_debug(
        #     f"{self.__class__.__name__}: INFO:  Messages: {messages}"
        # )

    async def tool_list_all(self, refresh: bool) -> List:  # caches and converts
        """
        Lists all available tools, converting them into a standardized OpenAPI-compatible format for API documentation or LLM agent toolchains.

        This function retrieves tool information from all connected MCP clients, caches the results, and converts each tool into a format compatible with OpenAPI/Swagger specifications or LLM agent toolchains. The function handles both cached and fresh tool data, ensuring efficient retrieval while maintaining consistent formatting.

        Args:
            refresh (bool): If True, forces a refresh of tool data from all MCP clients. Otherwise, uses cached data if available.

        Returns:
            List[Dict]: A list of tool dictionaries, each formatted according to the OpenAPI function schema. Each dictionary contains:
                - "type": "function" (str): Indicates this is a function-type tool.
                - "function": A dictionary with:
                    - "name" (str): The tool's name.
                    - "description" (str): The tool's description.
                    - "parameters": A schema object describing the tool's inputs, following OpenAPI's format.

        Note:
            - The function caches tool data under the keys "tools" and "function_map_tools" for performance optimization.
            - Tool data is converted using the `_convert_tool_format` helper function, which standardizes the schema for API compatibility.
            - If no tools are found from any MCP client, the function returns an empty list.
        """

        def _convert_tool_format(tool: Dict) -> Dict:
            """
            Convert a tool dictionary into an OpenAPI-compatible function schema format.

            This function transforms a tool's internal representation (typically from a tool registry or metadata system)
            into a standardized format suitable for API documentation or tool calling interfaces, such as those used in
            OpenAPI/Swagger specifications or LLM agent toolchains.

            Args:
                tool (Dict): A dictionary containing tool metadata with at least the following keys:
                    - 'name' (str): The name of the tool.
                    - 'description' (str): A human-readable description of the tool's purpose.
                    - 'inputSchema' (Dict): A schema describing the tool's input parameters. Must contain:
                        - 'properties' (Dict): A dictionary mapping parameter names to their type definitions.
                        - 'required' (List[str] or bool): A list of required parameter names, or a boolean indicating if any parameters are required.

            Returns:
                Dict: A dictionary formatted according to the OpenAPI function schema, containing:
                    - "type": "function" (str): Indicates this is a function-type tool.
                    - "function": A dictionary with:
                        - "name" (str): The tool's name.
                        - "description" (str): The tool's description.
                        - "parameters": A schema object describing the tool's inputs, following OpenAPI's format.

            Example:
                Input:
                {
                    "name": "read_file",
                    "description": "Read the complete contents of a file from the file system.",
                    "inputSchema": {
                        "properties": {
                            "path": {"type": "string"}
                        },
                        "required": ["path"]
                    }
                }

                Output:
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read the complete contents of a file from the file system.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"}
                            },
                            "required": ["path"]
                        }
                    }
                }

            Note:
                - The output schema strictly follows OpenAPI function schema format.
                - The 'inputSchema' is expected to conform to standard JSON Schema format for properties and required fields.
                - If 'required' is not provided, it defaults to an empty list.
            """
            if isinstance(tool, dict):  # returned from cache
                return {
                    "type": "function",
                    "function": {
                        "name": tool.get("name"),
                        "description": tool.get("description"),
                        "parameters": {
                            "type": "object",
                            "properties": tool.get("inputSchema", {}).get(
                                "properties", []
                            ),
                            "required": tool.get("inputSchema", {}).get(
                                "required", False
                            ),
                        },
                    },
                }
            else:  # returned from api
                return {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": {
                            "type": "object",
                            "properties": tool.inputSchema.get("properties", []),
                            "required": tool.inputSchema.get("required", False),
                        },
                    },
                }

        # tools cached under
        sk_tools_og: str = "tools"
        # mappings cached under
        sk_fname_map_tool: str = "function_map_tools"

        all_tools = []

        cached_tools_og = Cache.key_get(self.sfile, sk_tools_og, {})
        should_upcache = False

        # fetch the tools from API or use cache
        for name, mcp in self.clients.items():
            if refresh or not cached_tools_og.get(name, None):
                # TODO: dynamic tools support
                tool_og = await mcp.tool_list()
                self.utils.print_debug(
                    f"{self.__class__.__name__}: INFO: fetched {len(tool_og)} tools from <{name}>: {[t.name for t in tool_og]}"
                    # f"{self.__class__.__name__}: INFO: found {name} tools: {json.dumps(tool_og, indent=2)}"
                )
                cached_tools_og[name] = [
                    {
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": t.inputSchema,
                        "outputSchema": t.outputSchema,
                    }
                    for t in tool_og
                ]
                should_upcache = True  # if atleast one tools-list is fetched, consider cache is changed
            else:
                tool_og = cached_tools_og.get(name, None)

            conv_tool = [_convert_tool_format(t) for t in tool_og]
            if conv_tool:
                # self.utils.print_debug(
                #     f"{self.__class__.__name__}: INFO: found {len(conv_tool)} tools from <{name}>: {[t['function']['name'] for t in conv_tool]}"
                #     # f"{self.__class__.__name__}: INFO: found {name} tools: {json.dumps(conv_tool, indent=2)}"
                # )
                # Map tool names to their MCP instances
                for tool_info in conv_tool:
                    if "function" in tool_info and "name" in tool_info["function"]:
                        self.fname_map_tool[tool_info["function"]["name"]] = name

                all_tools.extend(conv_tool)

        # for any tool removed from configuration, we also invalidate its tools-list from cache
        if len(cached_tools_og.keys()) != len(self.clients.keys()):
            for k in tuple(cached_tools_og.keys()):
                if not self.clients.get(k):
                    _t = cached_tools_og.pop(k)
                    self.utils.print_debug(
                        f"{self.__class__.__name__}: INFO: removed {len(_t)} tools of <{k}>: {[t['name'] for t in _t]}"
                    )
            should_upcache = True

        # update cache so we don't need to refetch next time
        if should_upcache:
            Cache.key_set(self.sfile, sk_tools_og, cached_tools_og)
            Cache.key_set(self.sfile, sk_fname_map_tool, self.fname_map_tool)
        else:
            # reload mappings from cache with fallback
            self.fname_map_tool = Cache.key_get(
                self.sfile, sk_fname_map_tool, self.fname_map_tool
            )

        return all_tools

    async def tool_call(self, tc: Dict[str, any]) -> Dict[str, any]:
        """
        Async tool call handler that processes function calls from the LLM and executes them using registered clients.

        This method receives a tool call object (`tc`), extracts the function name and arguments, and attempts to execute the corresponding tool function via a registered client. It returns a response object with the tool's result or an error message if the tool is not found or an exception occurs.

        Parameters:
            tc (Dict[str, any]): The tool call object containing:
                - "function" (Dict[str, any]): The function to call, including its name and arguments.
                - "id" (str): The unique identifier for the tool call.

        Returns:
            Dict[str, any]: A response dictionary with:
                - "role" (str): Always set to "tool".
                - "tool_call_id" (str): ToolCall ID.
                - "name" (str): Function name that is called.
                - "content" (List[Dict[str, any]] or str): The tool's result as a list of text items, or an error message if execution failed.

        Raises:
            Exception: If an unexpected error occurs during tool call execution.

        Example:
            Assuming a tool call for 'read_file' with argument {'path': '/path/to/file.txt'}:
            >>> tc = {"function": {"name": "read_file", "arguments": "{\"path\": \"/path/to/file.txt\"}"}, "id": "tool_call_123"}
            >>> result = await tool_call(tc)
            >>> print(result["content"][0]["text"])  # Content of the file

        Note:
            This method assumes that tool names are mapped to client instances via `self.fname_map_tool`. If a tool name is not found in the map, an error message is returned.
        """
        resp = {"role": "tool", "content": None}
        try:
            resp["name"] = tc["function"]["name"]
            resp["tool_call_id"] = tc["id"]
            fargs = tc["function"]["arguments"]
            fargs = json.loads(fargs) if fargs else {}
            self.utils.print_debug(
                f"{self.__class__.__name__}: INFO: Processing tool_call {resp['name']} {fargs}"
            )
            if resp["name"] in self.fname_map_tool:
                client = self.clients[self.fname_map_tool[resp["name"]]]
                result = await client.tool_call(resp["name"], fargs)
                resp["content"] = [{"type": "text", "text": result.content[0].text}]
            else:
                resp["content"] = f"ERROR: {resp['name']} not found. skipped"
        except Exception as e:
            resp["content"] = f"ERROR: {e}"
        finally:
            return resp

    def check_user_approval(self, tool_calls: List[any]) -> Set[str]:
        """
        Determine which tools require approval based on configured allow/deny lists.

        This method evaluates a list of tool calls against configurable approval rules.
        It returns a set of function names that must be approved before execution.

        Parameters:
            -----------
        tool_calls : List[any]
            List of tool call objects. Each object should contain a 'function' key
            with a 'name' field indicating the tool name.

        Returns:
        --------
        Set[str]
            A set of function names that require approval. This set is empty if no
            tools require approval based on the configured rules.

        Behavior:
        ---------
        - If `self.fnames_auto_approve` is non-empty:
            - If `"*"` or `".*"` is present, all tools are allowed (returns empty set).
            - If no tools are in `self.fnames_auto_approve`, all tools are allowed (returns empty set).
            - Otherwise, return the set difference between all used
              tools and `fnames_auto_approve`.

        - If `self.fnames_ask_approve` is non-empty:
            - If no tools in `tool_calls` match `self.fnames_ask_approve`, return empty set.
            - Otherwise, return the intersection of `fnames_ask_approve` and used tools.

        - If neither `self.fnames_ask_approve` nor `self.fnames_auto_approve` is provided, return all tools.

        Notes:
        ------
        - TODO: Currently uses simple string matching. Consider using regex for more fine-grained control.
        - Tool names are case-sensitive.
        - Wildcard support (`*`, `.*`) is not case-sensitive.
        """
        tools = set([t["function"]["name"] for t in tool_calls])
        self.utils.print_debug(f"{self.__class__.__name__}: INFO: USING TOOLS: {tools}")
        if len(self.fnames_auto_approve):
            dontask = set(
                [word.strip() for word in self.fnames_auto_approve.split(",")]
            )
            # TODO: use regex match for finer selection from individual tools
            if "*" in dontask or ".*" in dontask:  # anything is allowed
                return set()
            elif len(tools - dontask) == 0:  # if all tools are in dontask, then allowed
                return set()
            else:
                return tools - dontask

        if len(self.fnames_ask_approve):
            ask = set([word.strip() for word in self.fnames_ask_approve.split(",")])
            if len(ask & tools) == 0:
                return set()
            else:
                return ask & tools
        return tools

    async def cleanup(self) -> None:
        """
        Asynchronously clean up all connected MCP clients and finalize resources.

        This method iterates through all MCP clients stored in `self.clients`, calling their
        individual `cleanup()` method to release resources and close connections. After
        cleaning up all clients, if an `_exit_stack` is present, it is awaited for final
        resource cleanup (e.g., closing file handles, releasing locks, etc.), and then
        set to `None` to prevent future accidental use.

        This method is typically called during shutdown or when the object is being
        destroyed to ensure no resources are leaked.

        Example:
            await instance.cleanup()

        Notes:
            - Clients are processed in the order they were added to `self.clients`.
            - `_exit_stack` is expected to be an async context manager (e.g., from `contextlib.AsyncExitStack`).
            - This method does not return any value.

        Raises:
            None explicitly, but any exception raised by `mcp.cleanup()` or
            `_exit_stack.__aexit__()` will propagate upwards.

        Returns:
            None
        """
        for index, mcp in enumerate(self.clients.values()):
            # self.utils.print_debug(f"{self.__class__.__name__}: INFO: cleanup tool {index} {mcp.name}")
            await mcp.cleanup()
            # self.utils.print_debug(f"{self.__class__.__name__}: INFO: cleanup done: {mcp.name}")

        if self._exit_stack is not None:
            await self._exit_stack.__aexit__(None, None, None)
            self._exit_stack = None

    def cached_state_clear(self) -> None:
        """Clear the cached state dictionary."""
        Cache.state_clear(self.sfile)


class MCPClient(object):
    """
    Represents and controls individual MCP services
    """

    def __init__(self, exit_stack, name: str, cfg: Dict, utils: AIUtils) -> None:
        """
        Initialize the MCP Client with necessary dependencies and configuration.

        This constructor sets up the agent's core components, including session management,
        stream handling, and configuration parsing. It also ensures that all non-server-
        parameter keys from the configuration are extracted and stored for later use.

        Args:
            exit_stack (contextlib.ExitStack): Context manager to handle resource cleanup.
            name (str): The name of the MCP instance.
            cfg (Dict): Configuration dictionary containing MCP-specific settings.
            utils (AIUtils): Utility class providing helper methods.

        Attributes:
            utils (AIUtils): Utility class instance for helper methods.
            session (Optional[ClientSession]): HTTP session for making requests (initialized lazily).
            read_stream (Optional[Any]): Stream for reading input data (initialized lazily).
            write_stream (Optional[Any]): Stream for writing output data (initialized lazily).
            _exit_stack (contextlib.ExitStack): Context manager for resource cleanup.
            name (str): Name of the agent instance.
            type (str): Type of agent (default: "stdio").
            stateful (bool): Whether the server maintains state across interactions (default: False).
            prompts (Dict): Custom system/user promps that are injected into the message.
            cfg (Dict): Parsed configuration after extracting all non-server parameters.

        Notes:
            - The `type` attribute is derived from the configuration, defaulting to "stdio".
        """
        self.utils = utils

        self.session: Optional[ClientSession] = None
        self.read_stream: Optional[Any] = None
        self.write_stream: Optional[Any] = None

        self._exit_stack = exit_stack
        self.name: str = name
        self.type: str = cfg.pop("type", "stdio")
        # self.hooks: Dict[str, Any] = cfg.pop("hooks", {}) # TODO
        self.prompt: Dict[str, List[str]] = cfg.pop("prompt", {})
        # extra system/user prompts to inject into messages
        # self.resources: Dict[str, Any] = cfg.pop("resources", {}) # TODO

        self.stateful: bool = cfg.pop("stateful", False)
        cfg.pop("meta", False)  # never used, good place for comments

        # ensure all non-server-parameter keys are popped before this point
        self.cfg = cfg

        # signals configuration finished
        # delays any tool-call or tool-list until this point
        # i.e. we have acquired
        #   the respective client,
        #   the read/write streams
        #   a client session with the streams
        #   a successful initialize()
        self.ev_conf_done = asyncio.Event()
        self.timeout_conf_done = 5  # seconds

    async def _create_client_session(self, force: bool = False) -> bool:
        """
        Creates and initializes a client session based on the configured MCP type.

        This method sets up the appropriate client session according to the MCP type
        specified in the configuration (e.g., "http", "sse", or "stdio"). It handles
        the creation of HTTP clients, SSE clients, or stdio clients depending on the
        type, and initializes the session with the appropriate streams.

        Args:
            force (bool, optional): If True, forces re-creation of the session even if one exists.
                                    Defaults to False.

        Returns:
            bool: True if the session was successfully created and initialized, False otherwise.

        Raises:
            ValueError: If an unsupported MCP type is encountered.

        Notes:
            - If a session already exists and `force` is False, the method does nothing and returns False.
            - For "http" type, it creates an HTTP client with headers and timeout settings from config,
            and wraps it with a streamable client.
            - For "sse" type, it creates an SSE client using the provided config.
            - For "stdio" type, it configures environment variables if needed (for Docker), and
            creates a stdio client.
            - Unsupported types result in a debug warning and return False.
            - The session is initialized after setup, and remains active until .cleanup() is called.
        """
        if self.session and (not force):
            # client session already setup, just needs initialize
            pass  # return False

        elif self.type == "http":
            httpx_client = create_mcp_http_client(
                headers=self.cfg.get("headers", []),
                timeout=httpx.Timeout(
                    self.cfg.get("timeout", 10.0),
                    read=self.cfg.get("read_timeout", 300.0),
                ),
            )
            await self._exit_stack.enter_async_context(httpx_client)

            (
                self.read_stream,
                self.write_stream,
                _,
            ) = await self._exit_stack.enter_async_context(
                streamable_http_client(
                    url=self.cfg.get("url", ""),
                    http_client=httpx_client,
                    terminate_on_close=self.cfg.get("terminate_on_close", False),
                )
            )

        elif self.type == "sse":  # TODO needs testing
            (
                self.read_stream,
                self.write_stream,
            ) = await self._exit_stack.enter_async_context(sse_client(**self.cfg))

        elif self.type == "stdio":
            if self.cfg.get("command", "") == "docker":
                # ensure container always has a predefined name so we can stop/kill it after
                has_container_name = False
                container_name = f"vimcp_{self.name}"  # default container name
                for a in self.cfg["args"]:
                    # parse name from '--name=container-name' arg
                    if a.startswith("--name="):
                        has_container_name = True
                        break
                if not has_container_name:
                    # assumes first argument is always "run"
                    self.cfg["args"].insert(1, f"--name={container_name}")

                # docker container needs its env vars
                if self.cfg.get("env", {}).values():
                    self._container_set_env()

            # will leak secrets, only enable for debug
            # self.utils.print_debug(f"{self.__class__.__name__}: WARN: MCP type {self.type} running {self.cfg}")
            (
                self.read_stream,
                self.write_stream,
            ) = await self._exit_stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(**self.cfg), errlog=asyncio.subprocess.DEVNULL
                )
            )

        else:
            self.utils.print_debug(
                f"{self.__class__.__name__}: WARN: MCP type {self.type} unsupported {self.cfg}, skipped."
            )
            self.ev_conf_done.set()
            return False

        self.session = await self._exit_stack.enter_async_context(
            ClientSession(self.read_stream, self.write_stream)
        )

        # Now the session is alive until .cleanup()
        await self.session.initialize()
        self.ev_conf_done.set()
        return True

    async def tool_call(self, fname: str, fargs: any) -> Dict[str, any]:
        """
        Asynchronously calls a tool by name with provided arguments.

        This method is responsible for invoking a tool registered within the session.
        If no session is currently active, it creates one by calling `_create_client_session`
        with force=True. After ensuring a session exists, it delegates the actual tool
        invocation to the session's `call_tool` method.

        Parameters:
            fname (str): The name of the tool to be called.
            fargs (any): The arguments to pass to the tool. This can be any valid Python
                        argument type supported by the tool.

        Returns:
            str: The result returned by the tool's `call_tool` method, typically a string
                representation of the output.

        Raises:
            Exception: If the tool call fails for any reason (e.g., tool not found, invalid
                    arguments, network issues, etc.), the underlying `call_tool` method may
                    raise an exception which will propagate up.

        Notes:
            - This method is designed to be used in asynchronous contexts.
            - It ensures that a session is created before attempting to call a tool, which
            is necessary for state management and tool access.
            - The `force=True` parameter in `_create_client_session` ensures that a new
            session is created even if one already exists.
            - This method does not handle retries; if a tool call fails, it is the caller's
            responsibility to decide whether to retry or handle the error.
        """
        if self.session is None:
            await self._create_client_session(force=True)
            # TODO: retry?
        await asyncio.wait_for(self.ev_conf_done.wait(), self.timeout_conf_done)
        return await self.session.call_tool(fname, fargs)

    async def tool_list(self) -> List:
        """
        Asynchronously retrieves and formats the list of available tools in the current session.

        This method first ensures that a client session is active by creating one if necessary.
        It then fetches the list of tools from the session and converts each tool into a standardized
        OpenAPI-compatible function format, including name, description, and parameters schema.

        The returned list of tools is structured as a list of dictionaries, each containing:
        - "type": Always set to "function".
        - "function": A dictionary with:
            - "name": The tool's name.
            - "description": A brief description of the tool's purpose.
            - "parameters": An object defining the input parameters schema, including:
                - "type": Always "object".
                - "properties": A dictionary mapping parameter names to their schema definitions.
                - "required": A boolean indicating whether the parameters are required.

        This method is typically used to expose available tools in a standardized format for
        external systems or for introspection purposes.

        Returns:
            List[Dict]: A list of tool descriptions in OpenAPI function format.
        """
        if self.session is None:
            await self._create_client_session()
        await asyncio.wait_for(self.ev_conf_done.wait(), self.timeout_conf_done)
        # TODO: selective listing based on context / filetype / project ??
        # TODO: then why not bm25?
        resp = await self.session.list_tools()
        tools = resp.tools
        # tools is not serializable at this point
        return tools
        # return [t["function"]["name"] for t in tools]

    def _container_set_env(self) -> None:
        """
        Inserts environment variables into the container run command arguments, right after the 'run' argument.

        This method is designed to handle environment variable injection for container execution. It:
        - Checks if 'run' is present in the configuration's 'args' list.
        - Locates the position of 'run' in the args list.
        - Iterates over the 'env' configuration dictionary, constructing environment variable arguments.
        - Skips environment variables whose keys start with '!', as these are typically meant to be excluded.
        - Avoids duplicate environment variable arguments already present in args.
        - Inserts the constructed environment variable arguments immediately after the 'run' argument.

        Note:
            - Environment variables are added in the format '-e KEY=VALUE'.
            - This ensures that environment variables are properly passed to the container at runtime.
        """
        # insert env vars into args, right after run
        if "run" in self.cfg.get("args", []):
            pos = self.cfg.get("args").index("run")
            envargs = []
            for k, v in self.cfg.get("env").items():
                envarg = f"{k}={v}"
                # skip keys starting with a (!) bang e.g. "!NOTINENV=123"
                # skip duplicates
                if (not k.startswith("!")) and (envarg not in self.cfg.get("args")):
                    envargs.append("-e")
                    envargs.append(f"{k}={v}")
            self.cfg.get("args")[pos + 1 : pos + 1] = envargs

    async def _container_cleanup(self) -> None:
        """
        Clean up Docker container associated with the current session by forcibly stopping or removing it.

        This method is designed to ensure that Docker containers used during the session are properly cleaned up
        to avoid session/stream loss and subsequent tool-call failures. It follows these steps:

        1. Constructs a default container name based on `self.name`, or extracts a custom name from
        command-line arguments if provided via `--name=container-name`.
        2. Checks if the container is currently running using `docker ps`.
        3. If the container is running:
        - If `--rm` is present in the command-line arguments, it stops the container immediately.
        - Otherwise, it removes the container forcefully.
        4. Logs actions for debugging purposes.
        5. Gracefully handles any exceptions that may occur during cleanup.

        Notes:
            - This method is asynchronous and relies on `Helpers.run_shell` and `Helpers.run_silent` for executing Docker commands.
            - It is intended to be called during cleanup or shutdown to prevent resource leaks.
            - If no container is found, no action is taken.

        Raises:
            Exception: If an error occurs during container cleanup (e.g., Docker CLI fails or unexpected behavior).

        Related Issues:
            - https://github.com/anthropics/claude-code/issues/29058
            - https://github.com/modelcontextprotocol/python-sdk/issues/2231
        """
        container_name = f"vimcp_{self.name}"  # default container name
        for a in self.cfg["args"]:
            # parse name from '--name=container-name' arg
            if a.startswith("--name="):
                container_name = a.split("=")[-1]
                break
        try:
            # check if container actually running before stopping
            (rc, stdout, stderr) = await Helpers.run_shell(
                " ".join(["docker", "ps", "-q", "-f", f"name={container_name}"])
            )
            # self.utils.print_debug(f"{self.__class__.__name__}: INFO: Exited with {rc}")
            if rc == 0 and stdout:  # means container running
                # self.utils.print_debug(f"{self.__class__.__name__}: INFO: container_name:{container_name}, id:{stdout}")
                # remove container by default, but prefer stop if --rm in args
                if "--rm" in self.cfg["args"]:
                    # self.utils.print_debug(
                    #     f"{self.__class__.__name__}: INFO: Stopping docker container: {container_name}"
                    # )
                    # expects --rm in args
                    await Helpers.run_silent(
                        "docker", "stop", "--time=0", container_name
                    )
                    self.utils.print_debug(
                        f"{self.__class__.__name__}: INFO: Stopped docker container: {container_name}"
                    )
                else:
                    # self.utils.print_debug(
                    #     f"{self.__class__.__name__}: INFO: Removing docker container: {container_name}"
                    # )
                    await Helpers.run_silent("docker", "rm", "-f", container_name)
                    self.utils.print_debug(
                        f"{self.__class__.__name__}: INFO: Removed docker container: {container_name}"
                    )
            # if stderr:
            #     self.utils.print_debug(f"{self.__class__.__name__}: [stderr]\n{stderr}")
        except Exception as e:
            self.utils.print_debug(
                f"{self.__class__.__name__}: ERROR: error stopping client ({self.name}) container ({container_name}): {e}"
            )

    async def cleanup(self) -> None:
        """
        Performs cleanup operations for the MCP instance.

        This method is responsible for stopping any running MCP client (if applicable)
        and closing any active streams. It also clears the session reference.

        Important Notes:
        - Only stops Docker containers if the command is set to "docker" and the instance is not stateful.
        - Does NOT work for stateful docker containers.
        - Clears the session reference to prevent memory leaks or stale references.

        Behavior:
        - If configured to use Docker and not stateful: calls `_container_cleanup()`.
        - Sets `self.session` to None to clear the session.

        This method is typically called when the instance is being shut down or released.
        """

        # self.utils.print_debug(f"{self.__class__.__name__}: INFO: Cleanup: {self.name}")

        # stop stdio containers since docker seems to gobble up the signals
        # this will NOT work for stateful containers
        if (self.cfg.get("command", "") == "docker") and not self.stateful:
            await self._container_cleanup()

        # await self.read_stream.aclose()
        # await self.write_stream.aclose()

        self.session = None


class Cache:
    """
    Caches data to reduce calls to MCP Servers, speeding up response
    time.
    """

    # TODO: read fresh-hours-threshold from "options.mcp_freshold_hours"?
    freshold: int = 12  # hours

    @staticmethod
    def state_clear(sfile: Path) -> None:
        """Clear the cached state dictionary."""
        Cache.state_set(sfile, {})  # write empty dict in file or remove file?
        # TODO: figure how to call this via a command to force a refetch/remap of all tools

    @staticmethod
    def state_get(sfile: Path) -> dict[str, any]:
        """
        Get the dictionary of cached tools.

        Returns a dictionary containing the cached tools state.
        The cache is refreshed if the cache file does not exist or is older than
        the configured refresh threshold (default: <Cache.freshold> hours). If the cache file
        is found and within the threshold, its content is loaded as a JSON object.
        If the cache file is not found or if an error occurs during reading,
        an empty dictionary is returned as a fallback.

        Note: Currently, the cache is refreshed on a fixed schedule (every
        `freshold` hours) since there's no support for on-demand deletion or
        refresh. This behavior may be improved in the future.

        Args:
            sfile (Path): The file to be read.

        Returns:
            dict[str, any]: A dictionary representing the cached tools state.

        """
        bydef = {}  # default value

        # force-refresh the cache every freshold hours until
        # we can find a way to delete/refresh file on demand
        if not Helpers.file_exists(sfile, freshold=Cache.freshold):
            return bydef
        return Helpers.json_read(sfile, bydef=bydef)

    @staticmethod
    def state_set(sfile: Path, state: dict[str, any]) -> bool:
        """
        Set the cached state dictionary. Validates input type.

        Args:
            sfile (Path): The file to be saved.
            state (dict[str, any]): The state dictionary to be saved.

        Returns:
            bool: True if the state was successfully saved, False if no sfile is set.

        Raises:
            TypeError: If the provided state is not a dictionary.

        Description:
            This method attempts to save the provided state dictionary to a file specified by sfile.
            It first checks if sfile is set; if not, it returns False.
            It then validates that the state is a dictionary, raising a TypeError if not.
            Finally, it writes the state to the file using Helpers.json_write with pretty formatting.
        """
        if not isinstance(state, dict):
            raise TypeError("state must be a dictionary")
        return Helpers.json_write(sfile, state, pretty=True)

    @staticmethod
    def key_get(sfile: Path, key: str, bydef: any = None) -> any:
        """
        Retrieve a value from the cache associated with a given file path and key.

        This method fetches the cached state for the specified file path and returns the value
        associated with the given key. If the key does not exist in the cache, it returns the
        default value provided by the 'bydef' parameter.

        Parameters:
        -----------
        sfile : Path
            The file path for which the cache state is to be retrieved.
        key : str
            The key for which the value is to be fetched from the cache.
        bydef : any, optional
            The default value to return if the key is not found in the cache.
            Defaults to None.

        Returns:
        --------
        any
            The value associated with the key in the cache, or the default value if the key is not found.
        """
        return Cache.state_get(sfile).get(key, bydef)

    @staticmethod
    def key_set(sfile: Path, key: str, val: any) -> bool:
        """
        Sets a key-value pair in the cache state file.

        This method retrieves the current state from the specified cache file,
        updates the value for the given key, and writes the updated state back
        to the file. If the file does not exist or any error occurs during
        reading/writing, it will return False.

        Parameters:
            sfile (Path): The path to the cache state file.
            key (str): The key to set in the cache state.
            val (any): The value to assign to the key.

        Returns:
            bool: True if the key-value pair was successfully set, False otherwise.
        """
        s = Cache.state_get(sfile)
        s[key] = val
        return Cache.state_set(sfile, s)


class Helpers:
    @staticmethod
    def envsubst(
        data: any,
        mappings: Mapping[str, str] = os.environ,
        secrets: Mapping[str, str] = None,
        _simple_re: any = re.compile(r"(?<!\\)\$([A-Za-z0-9_]+)"),
        _extended_re: any = re.compile(r"(?<!\\)\$\{([A-Za-z0-9_]+)((:?-)([^}]+))?\}"),
    ) -> any:
        """
        Substitute environment variables in a string or nested data structure.

        This function recursively replaces environment variables in the provided data (string, list, or dict) using
        the provided `mappings` and optionally `secrets`. It supports both simple `$VARIABLE` and extended
        `$${VARIABLE:default}` syntax, where the latter allows specifying a default value if the variable is unset or empty.

        Args:
            data (any): The data (string, list, or dict) to process. Nested structures are supported.
            mappings (Mapping[str, str], optional): A dictionary of variable names to their values.
                Defaults to `os.environ`. Additional mappings can be added, including UID/GID.
            secrets (Mapping[str, str], optional): A dictionary of secret variable names and their values.
                These will be merged into `mappings`. Defaults to None.
            _simple_re (any, optional): Compiled regex pattern for simple variable substitution ($VAR).
                Defaults to a compiled regex that matches unbracketed environment variables.
            _extended_re (any, optional): Compiled regex pattern for extended variable substitution
                ($${VAR:default} or $${VAR:-default}). Defaults to a compiled regex for extended syntax.

        Returns:
            any: The processed data with environment variables substituted. Returns the original data if no substitutions are needed.

        Behavior:
            - For strings: Replaces `$VAR` and `${VAR:default}` syntax with their corresponding values from `mappings` or `secrets`.
            If a variable is not found, it is replaced with an empty string for simple variables, or the specified default for extended variables.
            - For lists: Recursively processes each item in the list.
            - For dictionaries: Recursively processes each value in the dictionary.
            - For other types: Returns the data unchanged (no substitution performed).

        Example:
            data = "Hello $USER, your UID is $UID"
            mappings = {"USER": "john", "UID": "1001"}
            result = envsubst(data, mappings)
            # result = "Hello john, your UID is 1001"

        Note:
            - The function automatically includes `UID` and `GID` from the current process if not present in mappings.
            - Supports command-line argument substitution for numeric variable names (e.g., `$1`, `$2`).
            - Extended syntax with `:-` or `-` defaults is handled only for `$${VAR:default}` format.
            - If a regex pattern does not match, the original string is preserved.
            - Non-string data types are returned unchanged.

        Raises:
            RuntimeError: If an unexpected syntax is matched in extended variable substitution.
        """
        mappings = {
            **mappings,
            # no UID/GID in os.environ
            **{"UID": str(os.getuid()), "GID": str(os.getgid())},
        }

        # merge secrets with mappings
        if secrets and isinstance(secrets, dict):
            mappings = {**mappings, **secrets}

        def _resolve_var(var_name, default=None):
            """
            Resolve a variable name by attempting to interpret it as an integer index for command-line arguments,
            or as a key in a provided mappings dictionary.

            Parameters:
                var_name (str): The variable name to resolve.
                default (any, optional): The default value to return if the variable cannot be resolved.
                                        Defaults to None.

            Returns:
                any: The resolved value.
                    - If `var_name` can be interpreted as an integer, attempts to return the corresponding
                    command-line argument (sys.argv[index]).
                    - If the index is out of range, returns the default value.
                    - If `var_name` cannot be interpreted as an integer, returns the value from `mappings`
                    if the key exists, otherwise returns the default value.

            Raises:
                None: This function does not raise exceptions under normal operation, but relies on
                    sys.argv and mappings being appropriately defined in context.
            """
            try:
                index = int(var_name)
                try:
                    return sys.argv[index]
                except IndexError:
                    return default
            except ValueError:
                return mappings.get(var_name, default)

        def _repl_simple_env_var(m):
            """
            Replace a simple environment variable placeholder in a string with its resolved value.

            This function is designed to be used as a replacement function within regular expression
            substitution operations. It extracts the variable name from a match (e.g., '$VAR_NAME')
            and returns the resolved value of that variable. If the variable is not found or cannot
            be resolved, it returns an empty string.

            Args:
                m (re.Match): A match object from a regular expression that captured a variable
                            placeholder (e.g., '$VAR_NAME'). The captured group is assumed to
                            contain the variable name.

            Returns:
                str: The resolved value of the environment variable, or an empty string if
                    the variable is not found or cannot be resolved.

            Example:
                If the input string is "Hello $USER" and the environment variable 'USER' is set
                to 'alice', this function will return "Hello alice".

            Note:
                This function assumes that `_resolve_var` is a defined helper function that
                resolves environment variables by name.
            """
            var_name = m.group(1)
            return _resolve_var(var_name, "")

        def _repl_extended_env_var(m):
            """
            Replace extended environment variable expressions in a string.

            This function handles extended environment variable syntax, including:
            - Default values using `:-` (use default if variable is unset or empty)
            - Default values using `-` (use default if variable is unset)
            - Unset variables with no default (returns empty string)

            Args:
                m (re.Match): Match object from a regex that captures:
                    - `var_name` (group 1): The environment variable name
                    - `default_spec` (group 2): The default specification part (if any)
                    - `operator` (group 3): The operator, either ":-" or "-"
                    - `default` (group 4): The default value (if provided)

            Returns:
                str: The resolved value of the environment variable, or the default if specified and applicable.

            Raises:
                RuntimeError: If an unexpected operator is encountered.

            Example:
                Given input: "${VAR:-default}" -> returns the value of VAR, or "default" if unset.
                Given input: "${VAR-default}" -> returns the value of VAR, or "default" if unset.
                Given input: "${VAR}" -> returns the value of VAR, or empty string if unset.
            """
            var_name = m.group(1)
            default_spec = m.group(2)
            if default_spec:
                default = m.group(4)
                default = _simple_re.sub(_repl_simple_env_var, default)
                if m.group(3) == ":-":
                    # use default if var is unset or empty
                    env_var = _resolve_var(var_name)
                    if env_var:
                        return env_var
                    else:
                        return default
                elif m.group(3) == "-":
                    # use default if var is unset
                    return _resolve_var(var_name, default)
                else:
                    raise RuntimeError("unexpected string matched regex")
            else:
                return _resolve_var(var_name, "")

        if isinstance(data, str):
            if "$" in data:  # otherwise dont modify
                # handle simple un-bracketed env vars like $FOO
                a = _simple_re.sub(_repl_simple_env_var, data)
                # handle bracketed env vars with optional default specification
                data = _extended_re.sub(_repl_extended_env_var, a)
        elif isinstance(data, list):
            for index, item in enumerate(data):
                data[index] = Helpers.envsubst(item, mappings)
        elif isinstance(data, dict):
            for key, val in data.items():
                data[key] = Helpers.envsubst(val, mappings)
        # else:
        #     cannot process
        return data

    @staticmethod
    def file_exists(fp: Path, freshold: Optional[int] = 0) -> bool:
        """
        Check if a file exists at the given path and optionally verify its freshness based on a time threshold.

        This static method evaluates whether a file exists at the specified path and, if a threshold is provided,
        ensures the file was modified within the last `freshold` hours (default: 0, meaning no freshness check).

        Parameters:
            fp (Path): The file path to check. Must be a valid path string or Path object.
            freshold (Optional[int], default=0): The number of hours the file must be newer than.
                If 0 or not provided, no freshness check is performed. If positive, the file must have been modified
                within the last `freshold` hours.

        Returns:
            bool: True if the file exists and meets the freshness criteria (if applicable); False otherwise.

        Raises:
            None: This method does not raise exceptions but returns False in case of failure.

        Notes:
            - Uses `os.path.exists` to check if the file path exists.
            - Expands environment variables and user home directory using `os.path.expandvars` and `os.path.expanduser`.
            - If `freshold` is specified, checks if the file's modification time is within the last `freshold` hours.
            - Returns False if any condition is not met.

        Example:
            >>> file_exists(Path("/home/user/file.txt"), freshold=24)
            True  # if file exists and was modified within the last 24 hours

            >>> file_exists(Path("/home/user/missing.txt"))
            False  # file does not exist
        """
        if not fp:  # must have filepath
            return False
        if not os.path.exists(os.path.expandvars(os.path.expanduser(fp))):  # must exist
            return False
        # if set, file must be fresher than fresh-hours-threshold
        if freshold and os.path.getmtime(os.path.expandvars(os.path.expanduser(fp))) < (
            time.time() - (freshold * 60 * 60)
        ):
            return False
        return True

    @staticmethod
    def json_read(fp: Path, bydef: Any = None) -> Dict[str, any]:
        """
        Safely reads a JSON file and returns its parsed content.

        This method attempts to read a JSON file from the specified path.
        If the file does not exist, or if the file is not valid JSON, it returns the default value provided by the `bydef` parameter.

        Parameters:
            fp (Path): The file path to the JSON file.
            bydef (Any, optional): The default value to return if the file does not exist or is invalid JSON. Defaults to None.

        Returns:
            Any: The parsed JSON content if successful, otherwise `bydef`.

        Raises:
            FileNotFoundError: If the file does not exist and `bydef` is not provided.
            json.JSONDecodeError: If the file exists but is not valid JSON.
        """
        try:
            if not Helpers.file_exists(fp):
                return bydef

            with open(
                os.path.expandvars(os.path.expanduser(fp)), "r", encoding="utf-8"
            ) as f:
                d = json.load(f)
                return d
        except (FileNotFoundError, json.JSONDecodeError) as e:
            # raise Exception(f"Could no read from {fp}. Error: {e}")
            return bydef

    @staticmethod
    def json_write(fp: Path, data: any, pretty: bool = False) -> bool:
        """
        Write JSON data to a file.

        This static method serializes Python data structures (dict, list, etc.) into JSON format and writes them to a specified file path. It supports pretty-printing with indentation for human-readable output.

        Parameters:
            fp (Path): The file path (as a Path object) where the JSON data will be written. The path will be expanded to handle user home directories and environment variables.
            data (any): The Python data structure to serialize. Must be serializable to JSON (dict, list, string, number, boolean, or None).
            pretty (bool, optional): If True, writes the JSON with indentation and sorted keys for readability. Defaults to False (compact format).

        Returns:
            bool: Returns True if the write operation succeeds, False otherwise. In case of failure, no exception is raised — instead, False is returned.

        Example:
            >>> from pathlib import Path
            >>> data = {"name": "Alice", "age": 30}
            >>> json_write(Path("output.json"), data, pretty=True)
            True

        Note:
            - The file is opened in write mode with UTF-8 encoding.
            - If an error occurs during writing (e.g., permission denied, invalid JSON data), the method silently returns False without raising an exception.
            - The method does not verify if the file already exists — it will overwrite it if it does.
        """
        try:
            with open(
                os.path.expandvars(os.path.expanduser(fp)),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    data,
                    f,
                    ensure_ascii=False,
                    indent=(2 if pretty else None),
                    sort_keys=pretty,
                )
            return True
        except Exception as e:
            # raise Exception(f"Could no read from {fp}. Error: {e}")
            return False

    @staticmethod
    def run_afun(loop, fn, *args, **kwargs):
        """
        Blocks until the function (called in async context) finishes then returns the result.

        Parameters:
            loop (asyncio.AbstractEventLoop): The event loop to run the coroutine on.
            fn (callable): The function to be called asynchronously. Must be a coroutine function.
            *args: Variable length argument list to pass to the function.

        Returns:
            Any: The result of the function call once completed.

        Note:
            This function runs the provided coroutine function in a thread-safe manner within the given event loop.
            It blocks the current thread until the coroutine completes, returning its result.
            This is useful when you need to run async functions from synchronous code.
        """
        return asyncio.run_coroutine_threadsafe(fn(*args, **kwargs), loop).result()

    @staticmethod
    async def run_shell(cmd: str):
        """
        Runs the shell command in the given event loop asynchronously.

        This function spawns a subprocess using asyncio.create_subprocess_shell to execute the provided command.
        It captures both stdout and stderr streams and waits for the process to complete.
        Returns a tuple containing the return code of the process, the stdout output (as bytes), and the stderr output (as bytes).

        Args:
            cmd (str): The shell command to execute.

        Returns:
            tuple: A tuple containing:
                - proc.returncode (int): The exit code of the subprocess (0 for success).
                - stdout (bytes): The standard output from the subprocess.
                - stderr (bytes): The standard error output from the subprocess.

        Example:
            result = await run_shell("ls -la")
            return_code, stdout, stderr = result
            if return_code == 0:
                print("Command succeeded:", stdout.decode())
            else:
                print("Command failed:", stderr.decode())
        """
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        return (proc.returncode, stdout, stderr)

    @staticmethod
    async def run_silent(*cmd) -> None:
        """
        Executes a shell command silently in the current event loop, discarding both stdout and stderr output.

        This function runs the provided command asynchronously and waits for it to complete.
        It does not return any output or capture stdout/stderr, making it suitable for background tasks
        where the result of the command is not needed (e.g., logging, cleanup, or monitoring).

        Parameters:
            *cmd (list of str): The command and its arguments as individual strings.
                                Example: `["echo", "Hello World"]`.

        Returns:
            None

        Example:
            await run_silent("rm", "-rf", "/tmp/temp_folder")
            await run_silent("systemctl", "restart", "nginx")
        """
        await asyncio.wait_for(
            (
                await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            ).wait(),
            5,
        )

    @staticmethod
    def run_task(loop, fn, *args, **kwargs):
        """
        Runs a task in the given asyncio event loop.

        This function wraps a callable (fn) with its arguments (*args) and schedules it to run
        in the specified asyncio event loop. It returns a `concurrent.futures.Future` object
        representing the result of the scheduled task.

        Parameters:
            loop (asyncio.AbstractEventLoop): The asyncio event loop in which to run the task.
            fn (callable): The function to be executed.
            *args: Variable positional arguments to be passed to the function.

        Returns:
            asyncio.Future: A future representing the execution of the task in the specified loop.
                            The future will eventually hold the result (or exception) of the function call.

        Example:
            loop = asyncio.get_event_loop()
            future = run_task(loop, my_async_function, arg1, arg2)
            result = future.result()  # Wait for task completion
        """
        return asyncio.run_coroutine_threadsafe(fn(*args, **kwargs), loop)

    @staticmethod
    async def wait_for_tasks(tasks) -> None:
        """
        Waits for a list of asyncio tasks to complete.

        This method takes a list of asyncio tasks and waits for all of them to finish
        without raising any exceptions. It returns immediately after all tasks complete.

        Parameters:
            tasks (List[asyncio.Task]): A list of asyncio tasks to wait for.

        Returns:
            None: This method does not return any value.

        Example:
            tasks = [asyncio.sleep(1), asyncio.sleep(2)]
            await wait_for_tasks(tasks)
        """
        return await asyncio.wait(tasks)
