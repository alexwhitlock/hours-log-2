FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ARG GIT_HASH=unknown
RUN echo "{\"gitHash\":\"${GIT_HASH}\"}" > /app/static/version.json
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "--timeout", "300", "app:app"]
