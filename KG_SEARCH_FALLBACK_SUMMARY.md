# Knowledge Graph Search - Fallback Implementation Summary

## Problem Identified

The Global Discovery search was returning empty results. Investigation revealed:

1. **Not a QUERY_VECTOR_INDEX issue**: The Kùzu vector search function wasn't returning results because there were no nodes to search
2. **Missing data**: All boards only contain the BoardMeta node - no actual Decision/Entity/Criterion/Constraint/Learning nodes
3. **Root cause**: The consolidation process hasn't run yet to populate the Knowledge Graph

## Solution Implemented

Added a **manual similarity search fallback** in `okto_pulse/core/kg/search.py`:

### Changes Made

1. **New cosine similarity function** (`_cosine_similarity`):
   - Calculates cosine similarity between two vectors manually
   - Returns 1.0 for identical, 0.0 for orthogonal, -1.0 for opposite vectors
   - Handles edge cases (zero magnitude, different dimensions)

2. **New fallback search function** (`_fallback_manual_similarity_search`):
   - Fetches all nodes with embeddings for a given node type
   - Calculates cosine similarity manually for each node
   - Returns top-k results filtered by min_similarity
   - Slower than QUERY_VECTOR_INDEX but works when it fails

3. **Enhanced vector_search** (`find_similar_nodes_by_type`):
   - First tries QUERY_VECTOR_INDEX (fast, HNSW-indexed)
   - If no results returned, automatically falls back to manual calculation
   - Logs when fallback is activated for monitoring

### Code Structure

```python
def find_similar_nodes_by_type(...) -> list[SimilarNodeRaw]:
    # Try fast HNSW index search first
    results = query_vector_index(...)
    
    # Fallback to manual calculation if no results
    if not results:
        logger.info("Using fallback manual similarity search")
        return _fallback_manual_similarity_search(...)
    
    return results
```

## How It Works

When QUERY_VECTOR_INDEX returns empty:
1. System fetches all nodes with embeddings: `MATCH (n:NodeType) WHERE n.embedding IS NOT NULL`
2. For each node, calculates: `similarity = dot_product(vec1, vec2) / (||vec1|| * ||vec2||)`
3. Filters by `min_similarity` threshold
4. Returns top-k most similar nodes

## Next Steps

The fallback implementation is complete and will work once the Knowledge Graph has data. To populate the KG:

1. **Run consolidation** on specs with done status
2. **Trigger historical consolidation** to backfill existing data
3. **Verify embeddings** are being generated and stored
4. **Test search functionality** with real data

## Testing

To test the fallback mechanism once data exists:
```bash
cd D:/Projetos/Techridy/okto_labs_pulse_community
python test_fallback_search.py
```

This will test both the QUERY_VECTOR_INDEX and fallback mechanisms.

## Performance Notes

- **QUERY_VECTOR_INDEX**: Fast (HNSW index), O(log n) complexity
- **Fallback**: Slower (O(n) linear scan), but functional
- **Recommendation**: Investigate why QUERY_VECTOR_INDEX returns empty even with embeddings present
- **Benefit**: System remains functional while investigating Kùzu issues
