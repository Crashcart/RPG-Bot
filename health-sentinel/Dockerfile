FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 58291
CMD ["gunicorn", "--bind", "0.0.0.0:58291", "--workers", "2", "--timeout", "10", "app:app"]
