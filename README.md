<p align="center">
  <img src="assets/Banner_Image.jpeg" alt="MyVector Banner" width="100%">
</p>

<p align="center">
  <strong>Vector Storage & Search Plugin for MySQL</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-1.0.0-blue?style=flat-square" alt="Version">
  <img src="https://img.shields.io/badge/C%2B%2B-17-blue?style=flat-square" alt="C++17">
  <img src="https://img.shields.io/badge/MySQL-8.0%20|%208.4%20|%209.0-blue?style=flat-square" alt="MySQL">
  <a href="https://github.com/askdba/myvector/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-GPL--2.0-brightgreen?style=flat-square" alt="License">
  </a>
  <a href="https://github.com/askdba/myvector/actions/workflows/ci.yml">
    <img src="https://github.com/askdba/myvector/actions/workflows/ci.yml/badge.svg" alt="CI">
  </a>
</p>

<p align="center">
  <a href="#-features">Features</a> •
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-installation">Installation</a> •
  <a href="#-usage">Usage</a> •
  <a href="#-docker">Docker</a> •
  <a href="#-documentation">Documentation</a>
</p>

---

## 📢 Announcements

🎤 **FOSDEM 2025** - MySQL Devroom Session (Brussels, Feb 1-2, 2025):
[Boosting MySQL with Vector Search: Introducing the MyVector Plugin](https://fosdem.org/2025/schedule/event/fosdem-2025-4230-boosting-mysql-with-vector-search-introducing-the-myvector-plugin/)

🤖 **ChatGPT** generated an excellent description of MyVector:
[View on ChatGPT](https://chatgpt.com/share/67b4af65-7d20-8011-aaa2-ff79442055b0)

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🔍 **ANN Search** | Approximate Nearest Neighbor search using HNSW algorithm |
| 📊 **KNN Search** | Brute-force K-Nearest Neighbor for exact results |
| 📏 **Multiple Metrics** | L2 (Euclidean), Cosine, Inner Product, Hamming distance |
| ⚡ **Real-time Updates** | Binlog-based online index updates |
| 💾 **Persistent Indexes** | Save and load indexes to/from disk |
| 🔌 **Native Plugin** | Seamless MySQL integration via UDFs |

---

## 🎬 Demo

[![asciicast](https://asciinema.org/a/673238.svg)](https://asciinema.org/a/673238)

---

## 🚀 Quick Start

### Using Docker (Recommended)

```bash
# Pull and run MySQL with MyVector pre-installed
docker run -d -p 3306:3306 \
  -e MYSQL_ROOT_PASSWORD=myvector \
  ghcr.io/askdba/myvector:mysql-8.4

# Connect
mysql -h 127.0.0.1 -u root -pmyvector
```

### Manual Installation

```bash
# Clone into MySQL plugin directory
cd mysql-server/plugin
git clone https://github.com/askdba/myvector.git

# Build
cd ../bld
cmake .. <your-cmake-options>
cd plugin/myvector && make

# Install
cp myvector.so /usr/local/mysql/lib/plugin/
mysql -u root -p mysql < ../../../plugin/myvector/sql/myvectorplugin.sql
```

---

## 📦 Installation

### Prerequisites

- MySQL 8.0, 8.4, or 9.0
- C++17 compatible compiler
- CMake 3.14+

### Build from Source

```bash
# Get the MyVector sources
cd mysql-server/plugin
git clone https://github.com/askdba/myvector.git

# Generate makefile for the new plugin
cd ../bld
cmake .. <other options used for this build>

# Build the plugin
cd plugin/myvector
make
```

### Install the Plugin

```bash
# Copy the plugin shared library
cp mysql-server/bld/plugin_output_directory/myvector.so /usr/local/mysql/lib/plugin/

# Register the plugin and create stored procedures
mysql -u root -p mysql < mysql-server/plugin/myvector/sql/myvectorplugin.sql
```

---

## ⚙️ Configuration

MyVector introduces two system variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `myvector_index_dir` | Directory for vector index files | `/mysqldata` |
| `myvector_config_file` | Path to MyVector config file | `myvector.cnf` |

### Config File Setup

Create a config file (e.g., `/mysqldata/myvector.cnf`):

```ini
myvector_user_id=<dbuser>
myvector_user_password=<password>
myvector_socket=/tmp/mysql.sock
```

> ⚠️ **Security**: This file should be readable **ONLY** by the `mysql` OS user.

The plugin connects to MySQL for:
- Reading the base table during index creation
- Receiving binlog events for online index updates

Required privileges: `SELECT` on base tables, `REPLICATION_CLIENT`, `REPLICATION_SLAVE`

---

## 📖 Usage

### Vector Distance Calculation

```sql
-- Compare semantic similarity between words
SELECT myvector_distance(
    (SELECT wordvec FROM words50d WHERE word = 'school'),
    (SELECT wordvec FROM words50d WHERE word = 'university')
) AS distance;
-- Result: 13.96 (closer = more similar)

SELECT myvector_distance(
    (SELECT wordvec FROM words50d WHERE word = 'school'),
    (SELECT wordvec FROM words50d WHERE word = 'factory')
) AS distance;
-- Result: 33.61 (farther = less similar)
```

### ANN Search (Fast, Approximate)

```sql
-- Find 10 words most similar to 'harvard' using HNSW index
SELECT word FROM words50d 
WHERE MYVECTOR_IS_ANN('test.words50d.wordvec', 'wordid', 
    myvector_construct('[...]'));

-- Results: harvard, yale, princeton, graduate, cornell, stanford...
```

### KNN Search (Exact, Brute-force)

```sql
-- Exact nearest neighbor search with distances
SELECT word, myvector_distance(wordvec, 
    (SELECT wordvec FROM words50d WHERE word='harvard')) AS dist
FROM words50d
ORDER BY dist 
LIMIT 10;
```

---

## 🐳 Docker

### Available Images

| Tag | MySQL Version | Description |
|-----|---------------|-------------|
| `ghcr.io/askdba/myvector:mysql-8.0` | 8.0.x | Stable LTS |
| `ghcr.io/askdba/myvector:mysql-8.4` | 8.4.x | Latest LTS |
| `ghcr.io/askdba/myvector:mysql-9.0` | 9.0.x | Innovation |
| `ghcr.io/askdba/myvector:latest` | 8.4.x | Default |

### Docker Compose

```yaml
version: '3.8'

services:
  myvector:
    image: ghcr.io/askdba/myvector:mysql-8.4
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

```bash
docker compose up -d
```

---

## 🧪 Testing

MyVector includes a comprehensive test suite using MySQL Test Runner (MTR):

```bash
# Navigate to MySQL build directory
cd mysql-server/bld/mysql-test

# Run MyVector test suite
./mysql-test-run.pl myvector --verbose
```

### Try the Stanford 50D Demo

```bash
cd examples/stanford50d
gunzip insert50d.sql.gz

mysql -u root -p test
mysql> source create.sql
mysql> source insert50d.sql
mysql> source buildindex.sql  -- Edit for your database/table first
mysql> source search.sql
```

---

## 📚 Documentation

| Document | Description |
|----------|-------------|
| [DEMO.md](docs/DEMO.md) | Amazon Product Catalog demo |
| [ANN_BENCHMARKS.md](docs/ANN_BENCHMARKS.md) | Performance benchmarks |
| [STRUCTURE.md](docs/STRUCTURE.md) | Project structure |
| [DOCKER_IMAGES.md](docs/DOCKER_IMAGES.md) | Docker image details |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guidelines |
| [CHANGELOG.md](CHANGELOG.md) | Version history |

---

## 🤝 Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

This project is licensed under the GNU General Public License v2.0 - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- [hnswlib](https://github.com/nmslib/hnswlib) - HNSW algorithm implementation
- MySQL Community for the excellent plugin architecture

---

<p align="center">
  <img src="assets/Logo_Image.jpeg" alt="MyVector Logo" width="80">
</p>

<p align="center">
  Made with ❤️ for the MySQL community
</p>
