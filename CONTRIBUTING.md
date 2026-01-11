# Contributing to MyVector

Thank you for your interest in contributing to MyVector! This document outlines our development workflow and guidelines.

## Development Workflow

```
┌─────────────────┐
│  1. Create Issue │ ← Document and track the feature/improvement
└────────┬────────┘
         ▼
┌─────────────────┐
│  2. Implement    │ ← Write code referencing the issue number
└────────┬────────┘
         ▼
┌─────────────────┐
│  3. Check Docs   │ ← Update README.md and relevant documentation
└────────┬────────┘
         ▼
┌─────────────────┐
│  4. Check Tests  │ ← Ensure all tests pass, add new tests
└────────┬────────┘
         ▼
┌─────────────────┐
│  5. Update       │ ← Document changes in CHANGELOG.md
│     CHANGELOG    │
└────────┬────────┘
         ▼
┌─────────────────┐
│  6. Release      │ ← Create PR, wait for CI, merge, tag release
│     Process      │
└─────────────────┘
```

## Step-by-Step Guide

### 1. Create Issue

Before starting work, create a GitHub issue to:
- Describe the feature, bug, or improvement
- Discuss the approach with maintainers
- Get feedback before investing time

**Issue Types:**
- 🐛 Bug Report
- ✨ Feature Request  
- 📚 Documentation
- 🔧 Maintenance

### 2. Implement

```bash
# Create a feature branch
git checkout -b feature/issue-42-add-cosine-distance

# Make your changes, commit with issue reference
git commit -m "Add cosine distance support

Implements cosine similarity distance metric for HNSW indexes.

Fixes #42"
```

**Commit Message Format:**
```
<type>: <short summary>

<detailed description>

Fixes #<issue-number>
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

### 3. Check Documentation

Update relevant documentation:
- [ ] `README.md` - If adding new features or changing usage
- [ ] `DEMO.md` - If changing demo instructions
- [ ] Code comments - For complex logic
- [ ] `myvectorplugin.sql` - If adding new SQL functions/procedures

### 4. Check Tests

```bash
# Build the plugin
cd mysql-server/bld/plugin/myvector
make

# Run the demo tests
mysql -u root -p test < demo/stanford50d/create.sql
mysql -u root -p test < demo/stanford50d/search.sql
```

**Test Checklist:**
- [ ] Existing tests pass
- [ ] New tests added for new functionality
- [ ] Edge cases covered
- [ ] Performance not degraded

### 5. Update CHANGELOG

Add your changes to `CHANGELOG.md` under `[Unreleased]`:

```markdown
## [Unreleased]

### Added
- Cosine distance support for HNSW indexes (#42)

### Fixed
- Memory leak in thread-local storage (#43)

### Changed
- Improved error messages for invalid vectors (#44)
```

**Categories:**
- `Added` - New features
- `Changed` - Changes in existing functionality
- `Deprecated` - Soon-to-be removed features
- `Removed` - Removed features
- `Fixed` - Bug fixes
- `Security` - Vulnerability fixes

### 6. Release Process

```bash
# Push your branch
git push origin feature/issue-42-add-cosine-distance

# Create Pull Request on GitHub
# - Reference the issue: "Fixes #42"
# - Wait for CI checks to pass
# - Request review from maintainers

# After merge, maintainers will:
# 1. Update CHANGELOG.md version header
# 2. Create git tag: git tag -a v1.1.0 -m "Release v1.1.0"
# 3. Push tag: git push origin v1.1.0
# 4. Create GitHub Release
```

## Code Style Guidelines

### C++ Style

```cpp
// Use descriptive names
int vectorDimension;  // Good
int vd;               // Bad

// Document public APIs
/**
 * Compute L2 (Euclidean squared) distance between two vectors.
 * @param v1 First vector
 * @param v2 Second vector  
 * @param dim Vector dimension
 * @return Squared Euclidean distance
 */
double computeL2Distance(const FP32* v1, const FP32* v2, int dim);

// Use the logging macros
debug_print("Loading index %s", name.c_str());  // Good
fprintf(stderr, "Loading index %s\n", name);     // Bad

// Use snprintf for safety
snprintf(buffer, sizeof(buffer), "...");  // Good
sprintf(buffer, "...");                    // Bad
```

### SQL Style

```sql
-- Use uppercase for SQL keywords
SELECT id, vector FROM products WHERE id = 1;

-- Comment stored procedures
DELIMITER $$
-- Build or rebuild a vector index from base table
-- @param index_name: Fully qualified index name (db.table.column)
-- @param id_column: Primary key column name
CREATE PROCEDURE mysql.myvector_index_build(...)
$$
```

## Getting Help

- 📖 [README.md](README.md) - Build and installation
- 🎥 [FOSDEM Talk](https://fosdem.org/2025/schedule/event/fosdem-2025-4230-boosting-mysql-with-vector-search-introducing-the-myvector-plugin/) - Overview and demos
- 💬 [GitHub Issues](https://github.com/pulbdb-ai/myvector/issues) - Questions and discussions

## License

By contributing, you agree that your contributions will be licensed under the GPL v2.0 License.
