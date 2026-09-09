FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir mysql-connector-python
COPY app.py .
CMD ["python", "app.py"]
