"""Remote MCP servers a run may use as tools, saved per stack.

A definition is a name, a URL and optional auth headers. Headers are secrets:
they are stored (as SSM SecureStrings when deployed), used to connect, and never
returned by the API or written into run/evaluation history. Runs reference
servers by id only (``RunRequest.mcp_servers``), and the process executing the
run -- the server, or the cloud evaluation worker -- resolves them from the
store at execution time.
"""
