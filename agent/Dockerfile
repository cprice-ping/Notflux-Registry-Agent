FROM python:3.12-slim

# Non-root user
RUN useradd -m -u 1000 agentuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py server.py ./

USER agentuser

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
