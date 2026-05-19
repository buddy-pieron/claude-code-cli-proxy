FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://www.npmjs.com/install.sh | sh && \
    npm install -g @anthropic-ai/claude-code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py claude_cli.py ./

EXPOSE 8070

CMD ["python", "server.py"]
