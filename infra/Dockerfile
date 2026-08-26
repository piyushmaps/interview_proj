FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY src ./src
COPY data/sample ./data/sample

RUN pip install --no-cache-dir -e .

EXPOSE 8000

# Expect FAISS index to be baked in or mounted at data/index/faiss
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
