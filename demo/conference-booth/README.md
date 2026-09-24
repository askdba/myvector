# MyVector Conference Booth Demo

A 3-minute, hands-on semantic-search demo built around the MyVector repo.

## Booth story

**Search products by meaning, not just keywords, while keeping the data and vector index inside MySQL.**

The demo has three beats:

1. **Keyword search**: search `waterproof jacket` and see literal matches.
2. **Semantic search**: search `something warm for a rainy hike` and retrieve relevant products even when those words are not present.
3. **Architecture reveal**: show that the product row and embedding live in MySQL, with MyVector providing the HNSW ANN index. No separate vector database.

MyVector's existing Amazon catalog demo uses 2 million products with 768-dimensional `all-mpnet-base-v2` embeddings and an HNSW index. This booth version deliberately uses a tiny deterministic dataset so it starts quickly and never depends on downloading a multi-gigabyte dataset during a conference.

## Run

```bash
docker compose up --build
```

Open http://localhost:8080.

## Booth flow

Use these searches in order:

- `waterproof jacket`
- `something warm for a rainy hike`
- `gift for someone who loves coffee`
- `lightweight travel charger`

For the final reveal, click **Show SQL** and point out that the semantic query is executed by MyVector's vector index in MySQL.

## Production-sized follow-up

For the full benchmark/demo, see `docs/DEMO.md`. It describes the Amazon catalog with 2 million rows, 768-dimensional embeddings, HNSW parameters, index build, and the Python semantic-search example.
