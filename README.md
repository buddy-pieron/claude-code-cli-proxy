# Claude Code Proxy

Turn your Claude Code subscription into a local OpenAI-compatible API.

Every request routes through the `claude` CLI binary on your machine, billing against your existing Claude Code subscription ($20/mo Pro or $100/mo Max) instead of per-token API pricing. Use Claude from any tool that speaks the OpenAI or Anthropic API format: Cursor, Continue, Open WebUI, LiteLLM, custom apps, etc.

## How It Works

```
Your App  -->  Claude Code Proxy  -->  claude CLI binary  -->  Anthropic
(OpenAI format)    (localhost:8070)     (your subscription)     (no extra cost)
```

The proxy spawns the `claude` CLI in print mode for each request, translates between OpenAI/Anthropic message formats and the CLI's stream-json output, and returns standard SSE streaming or JSON responses. Your app thinks it's talking to an OpenAI-compatible API. The CLI thinks it's running a normal session. Anthropic bills it against your subscription.

## Prerequisites

- **Python 3.10+**
- **Claude Code CLI** installed and authenticated
- **Active Claude Code subscription** (Pro or Max)

### Installing Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
```

Then authenticate:

```bash
claude
# Follow the OAuth flow in your browser
```

Verify it works:

```bash
claude --print "Hello"
```

## Installation

### Option 1: Clone and run

```bash
git clone https://github.com/legitlabs/claude-code-proxy.git
cd claude-code-proxy
pip install -r requirements.txt
python server.py
```

### Option 2: pip install

```bash
pip install git+https://github.com/legitlabs/claude-code-proxy.git
claude-code-proxy
```

### Option 3: Docker

```bash
docker build -t claude-code-proxy .
docker run -p 8070:8070 \
  -v ~/.claude:/root/.claude \
  claude-code-proxy
```

> **Note:** Mount your `~/.claude` directory so the container can access your CLI credentials.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_PROXY_HOST` | `127.0.0.1` | Bind address |
| `CLAUDE_PROXY_PORT` | `8070` | Listen port |
| `CLAUDE_PROXY_API_KEY` | *(empty)* | Optional Bearer token for auth |
| `CLAUDE_BIN` | *(auto-detect)* | Path to `claude` binary |

Copy `.env.example` to `.env` to configure:

```bash
cp .env.example .env
```

## API Endpoints

### `GET /health`

Health check. Returns `{"status": "ok"}` when the CLI binary is found.

### `GET /v1/models`

Lists available Claude models:

```json
{
  "object": "list",
  "data": [
    {"id": "claude-opus-4-7", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-sonnet-4-6", "object": "model", "owned_by": "anthropic"},
    {"id": "claude-haiku-4-5-20251001", "object": "model", "owned_by": "anthropic"}
  ]
}
```

### `POST /v1/chat/completions`

OpenAI-compatible chat completions. Supports streaming and non-streaming.

```bash
# Non-streaming
curl http://localhost:8070/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'

# Streaming
curl http://localhost:8070/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'
```

### `POST /v1/messages`

Anthropic Messages API format. Automatically translated to/from the OpenAI format internally.

```bash
curl http://localhost:8070/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-6",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Usage Examples

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8070/v1",
    api_key="not-needed",  # or your CLAUDE_PROXY_API_KEY
)

response = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Explain quantum computing in one paragraph"}],
)
print(response.choices[0].message.content)
```

### Python (streaming)

```python
stream = client.chat.completions.create(
    model="claude-sonnet-4-6",
    messages=[{"role": "user", "content": "Write a haiku about coding"}],
    stream=True,
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

### Cursor / Continue / Other IDEs

Point your IDE's OpenAI-compatible API settings to:

```
Base URL: http://localhost:8070/v1
API Key: (your CLAUDE_PROXY_API_KEY, or anything if auth is disabled)
Model: claude-sonnet-4-6
```

### Open WebUI

Add as an OpenAI-compatible connection:

```
URL: http://localhost:8070/v1
Key: (your CLAUDE_PROXY_API_KEY or "none")
```

## Running as a Service

### systemd (Linux)

```ini
# /etc/systemd/system/claude-code-proxy.service
[Unit]
Description=Claude Code Proxy
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/claude-code-proxy
ExecStart=/usr/bin/python3 server.py
Restart=on-failure
Environment=CLAUDE_PROXY_PORT=8070

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now claude-code-proxy
```

### launchd (macOS)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.legitlabs.claude-code-proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/claude-code-proxy/server.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

Save to `~/Library/LaunchAgents/com.legitlabs.claude-code-proxy.plist` and load:

```bash
launchctl load ~/Library/LaunchAgents/com.legitlabs.claude-code-proxy.plist
```

## How Billing Works

Claude Code subscriptions include usage allowances:
- **Pro ($20/mo):** Includes Opus, Sonnet, and Haiku usage
- **Max ($100/mo):** Higher rate limits, more Opus access

This proxy sends requests through the CLI, which uses your subscription's allowance. There are no additional API charges. If you hit your subscription's rate limit, requests will be queued or rejected by the CLI, and the proxy will return the error.

## Security Notes

- The proxy binds to `127.0.0.1` by default (localhost only). If you expose it on `0.0.0.0`, set `CLAUDE_PROXY_API_KEY` to prevent unauthorized access.
- The `claude` CLI runs with `--dangerously-skip-permissions` for non-interactive operation. This means the CLI can read/write files in allowed directories. The default allowed directories are `/tmp` and your home directory.
- Do not expose this proxy to the public internet without authentication.

## Limitations

- **Single-turn only.** The CLI starts fresh for each request. There is no persistent conversation state between API calls (conversation history is passed in the messages array each time, like any stateless API).
- **No tool use passthrough.** The CLI's built-in tools (file editing, bash, etc.) are available to the model during execution, but tool_calls are not exposed in the API response. The model's final text response is what you get.
- **Rate limits.** Bound by your Claude Code subscription's rate limits, not API rate limits.
- **Concurrency.** Each request spawns a separate CLI process. Heavy concurrent usage will consume more system resources and may hit subscription rate limits faster.

## License

MIT. See [LICENSE](LICENSE).
