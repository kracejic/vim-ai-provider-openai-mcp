let s:plugin_root = expand('<sfile>:p:h:h')

" Register the provider
cal vim_ai_provider#Register('openai-mcp', {
\  'script_path': s:plugin_root . '/py/openai_mcp.py',
\  'class_name': 'OpenAIMCPProvider',
\})
