FROM python:3.12-slim
WORKDIR /app
COPY agent/ ./agent/
COPY junior-academy/ ./junior-academy/
# stdlib only: no pip install needed. No secrets baked in:
# set GEMINI_API_KEY / OPENROUTER_API_KEY / HF_TOKEN in the host dashboard.
ENV PORT=7860
EXPOSE 7860
CMD ["python", "agent/server.py", "--host", "0.0.0.0", "--no-browser"]
