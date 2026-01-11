---
name: 🐛 Bug Report
about: Report a bug or unexpected behavior
title: '[BUG] '
labels: bug
assignees: ''
---

## Bug Description
A clear and concise description of the bug.

## Environment
- **MySQL Version**: [e.g., 8.0.35, 9.0.0]
- **OS**: [e.g., Ubuntu 22.04, macOS 14.0]
- **MyVector Version**: [e.g., 1.0.0 or commit hash]
- **Index Type**: [e.g., HNSW, KNN, HNSW_BV]

## Steps to Reproduce
1. Create table with '...'
2. Insert vectors '...'
3. Run query '...'
4. See error

## Expected Behavior
What you expected to happen.

## Actual Behavior
What actually happened.

## SQL to Reproduce
```sql
-- Minimal SQL to reproduce the issue
CREATE TABLE test_vectors (
  id INT PRIMARY KEY,
  vec MYVECTOR(type=HNSW,dim=128,size=10000,M=16,ef=100)
);

-- ... more SQL
```

## Error Messages
```
Paste any error messages from MySQL error log or client
```

## Additional Context
- Are you using online binlog updates?
- What is the approximate dataset size?
- Any other relevant configuration?
