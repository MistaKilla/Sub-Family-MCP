"""Sub-Family: a multi-model delegation family for coding agents, exposed over MCP.

The package is intentionally layered:

``config``      -- validated YAML registry of family members
``providers``   -- OpenAI-compatible HTTP clients (Ollama, LM Studio, vLLM, DeepSeek, ...)
``sessions``    -- persistent virtual conversation sessions (SQLite)
``security``    -- trust-aware secret redaction and policy checks
``service``     -- orchestration: ask, swarm, health, session management
``server``      -- MCP tool surface (thin wrappers around the service)
``cli`` / ``tui`` -- human-facing admin tooling
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
