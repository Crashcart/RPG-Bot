FROM python:3.12-alpine AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.12-alpine
WORKDIR /app
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
COPY app.py .
EXPOSE 58291
CMD ["gunicorn", "--bind", "0.0.0.0:58291", "--workers", "2", "--timeout", "10", "app:app"]
