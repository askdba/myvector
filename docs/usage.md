# Usage & API Reference

## Index Management

**Build an Index:**

```sql
CALL mysql.myvector_index_build('database.table.column', 'primary_key_column');
```

**Check Index Status:**

```sql
CALL mysql.myvector_index_status('database.table.column');
```

**Drop an Index:**

```sql
CALL mysql.myvector_index_drop('database.table.column');
```

**Save and Load an Index:**

```sql
-- Indexes are automatically saved. To load an index on startup:
CALL mysql.myvector_index_load('database.table.column');
```

## Vector Functions

**Create a Vector:**

```sql
SET @my_vector = myvector_construct('[1.2, 3.4, 5.6]');
```

**Calculate Distance:**

```sql
SELECT myvector_distance(@vec1, @vec2, 'L2');
```

**Display a Vector:**

```sql
SELECT myvector_display(wordvec) FROM words50d LIMIT 1;
```

## Docker Compose

```yaml
version: '3.8'

services:
  myvector:
    image: ghcr.io/askdba/myvector:mysql8.4
    ports:
      - "3306:3306"
    environment:
      MYSQL_ROOT_PASSWORD: myvector
      MYSQL_DATABASE: vectordb
    volumes:
      - myvector-data:/var/lib/mysql

volumes:
  myvector-data:
```

See [Docker Images](DOCKER_IMAGES.md) for the full list of available tags and
[Configuration](CONFIGURATION.md) for the full configuration reference.
