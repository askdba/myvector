---
name: ✨ Feature Request
about: Suggest a new feature or enhancement
title: '[FEATURE] '
labels: enhancement
assignees: ''
---

## Feature Description
A clear and concise description of the feature you'd like.

## Use Case
Describe the problem this feature would solve or the use case it enables.

**Example:**
> As a developer building a RAG application, I need to filter vector search results by metadata so that I can return only relevant documents.

## Proposed Solution
Describe how you envision this feature working.

```sql
-- Example SQL showing desired syntax/behavior
SELECT * FROM documents 
WHERE MYVECTOR_IS_ANN('db.docs.embedding', 'id', @query_vec)
  AND category = 'technology';  -- metadata filter
```

## Alternatives Considered
Any alternative solutions or features you've considered.

## Additional Context
- Links to similar features in other vector databases
- Performance considerations
- Backwards compatibility concerns

## Would you like to implement this?
- [ ] Yes, I'd like to submit a PR for this feature
- [ ] No, I'm just suggesting the idea
