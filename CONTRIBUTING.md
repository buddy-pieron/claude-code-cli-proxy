# Contributing to Claude Code CLI Proxy

Thanks for your interest! This project turns Claude Code subscriptions into local OpenAI-compatible APIs. Contributions of all kinds are welcome.

## Getting Started

```bash
git clone https://github.com/buddy-pieron/claude-code-cli-proxy.git
cd claude-code-cli-proxy
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
pip install pytest pytest-asyncio httpx
```

You'll need Claude Code CLI installed and authenticated:

```bash
npm install -g @anthropic-ai/claude-code
claude  # authenticate via browser
```

## Running Tests

```bash
pytest tests/ -v
```

Tests mock the CLI subprocess so you don't need a live Claude session to run them.

## Development Server

```bash
python server.py
# Listening on http://127.0.0.1:8070
```

## Submitting Changes

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Add or update tests for new functionality
4. Run `pytest tests/ -v` and make sure everything passes
5. Open a pull request with a clear description of what changed and why

## What We're Looking For

Check the [issues](https://github.com/buddy-pieron/claude-code-cli-proxy/issues) for tasks tagged `good first issue` or `help wanted`. Some areas where help is especially welcome:

- **Tool calling support** — translating OpenAI function calling to/from Claude CLI
- **Streaming improvements** — better SSE compliance, edge cases
- **Platform testing** — macOS, Windows, different Python versions
- **Documentation** — usage guides, integration examples
- **Performance** — connection pooling, caching, benchmarks

## Code Style

- Keep it simple. This is a small, focused project.
- No unnecessary abstractions or frameworks.
- Type hints are appreciated.
- Comments only when the "why" isn't obvious.

## Reporting Bugs

Open an issue with:
- What you expected vs. what happened
- Your OS, Python version, and Claude CLI version
- Relevant logs (set `CLAUDE_PROXY_LOG_LEVEL=DEBUG` for verbose output)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
