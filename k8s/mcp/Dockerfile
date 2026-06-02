FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8000

# Run as a non-root user
RUN adduser --disabled-password --no-create-home mcpuser
USER mcpuser

CMD ["python", "server.py"]
