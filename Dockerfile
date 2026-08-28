FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY main.py ./
COPY app ./app
COPY config ./config
COPY data ./data
COPY Dodo_IS_API_Reference_Sorted.zip ./
RUN pip install --no-cache-dir .
CMD ["python", "main.py"]
