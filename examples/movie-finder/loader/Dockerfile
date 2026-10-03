FROM python:3.12-slim
WORKDIR /loader
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# DuckDB's httpfs extension, fetched at build time so the loader needs no download for it later.
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')"
COPY load.py .
CMD ["python", "-u", "load.py"]
