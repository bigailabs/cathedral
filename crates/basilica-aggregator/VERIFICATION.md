# End-to-End Verification Guide

This document outlines the manual verification steps for the Basilica GPU Price Aggregator (Task 15).

## Pre-requisites

Before running the service with real data, ensure you have:

1. Valid DataCrunch OAuth2 credentials (client_id and client_secret)
2. Rust 1.80+ installed
3. The configuration file set up

## Verification Steps

### 1. Create Configuration File

```bash
cp config/aggregator.toml.example config/aggregator.toml
```

### 2. Configure OAuth2 Credentials

Edit `config/aggregator.toml` and add your DataCrunch credentials:

```toml
[providers.datacrunch]
enabled = true
client_id = "your-actual-client-id"
client_secret = "your-actual-client-secret"
```

### 3. Build the Service

```bash
just build-aggregator
```

**Expected**: Clean build with no errors

### 4. Run the Service

```bash
just aggregator
```

**Expected**:
- Server starts on port 8080
- Log message: "Basilica GPU Price Aggregator starting..."
- Log message: "Server listening on 0.0.0.0:8080"

### 5. Test Health Endpoint

In a separate terminal:

```bash
curl http://localhost:8080/health
```

**Expected**: JSON response with provider health status
```json
{
  "providers": [
    {
      "provider": "datacrunch",
      "is_healthy": true,
      "last_success_at": "2025-11-05T...",
      "last_error": null
    }
  ]
}
```

### 6. Test Provider Status Endpoint

```bash
curl http://localhost:8080/providers
```

**Expected**: Same response as health endpoint

### 7. Test GPU Prices Endpoint

```bash
curl http://localhost:8080/gpu-prices
```

**Expected**: JSON response with offerings array
```json
{
  "offerings": [...],
  "total_count": <number>
}
```

### 8. Test Filtering

```bash
# Filter by GPU type
curl "http://localhost:8080/gpu-prices?gpu_type=H100_80GB"

# Filter by availability
curl "http://localhost:8080/gpu-prices?available_only=true"

# Sort by price
curl "http://localhost:8080/gpu-prices?sort_by=price"

# Combine filters
curl "http://localhost:8080/gpu-prices?gpu_type=H100_80GB&available_only=true&sort_by=price"
```

**Expected**: Filtered and sorted results according to query parameters

### 9. Verify Database Creation

```bash
ls -la aggregator.db
```

**Expected**: SQLite database file exists

### 10. Verify Caching

Make two requests within 30 seconds:

```bash
curl http://localhost:8080/gpu-prices
# Wait 5 seconds
curl http://localhost:8080/gpu-prices
```

**Expected**: Second request should be faster (served from cache)
Check logs for messages about using cached data.

### 11. Run All Tests

```bash
just test-aggregator
```

**Expected**: All tests pass
- Unit tests for config validation
- Unit tests for GPU normalization
- Integration tests for service initialization
- Integration tests for database operations

## What Should Work

After completing these verification steps, you should confirm:

1. ✅ Service starts without errors
2. ✅ All three API endpoints respond correctly
3. ✅ DataCrunch API integration fetches real data
4. ✅ SQLite database is created and populated
5. ✅ Caching reduces API calls (cooldown timer works)
6. ✅ Query filtering works as expected
7. ✅ Sorting works correctly
8. ✅ Health checks report provider status
9. ✅ All automated tests pass
10. ✅ Graceful fallback to cached data on API errors

## Known Limitations (Phase 1 MVP)

- Only DataCrunch provider is implemented
- No metrics/monitoring (planned for Phase 4)
- No Docker deployment (planned for Phase 4)
- No price trend analysis (planned for Phase 3)

## Troubleshooting

### Service fails to start

- Check that `DATACRUNCH_CLIENT_ID` and `DATACRUNCH_CLIENT_SECRET` environment variables are set
- Verify config file exists and is valid TOML
- Check that port 8080 is not already in use

### API returns errors

- Verify OAuth2 credentials (client_id and client_secret) are valid
- Check network connectivity
- Review logs for detailed error messages
- Verify cached data by checking database: `sqlite3 aggregator.db "SELECT COUNT(*) FROM gpu_offerings;"`

### Tests fail

- Ensure you have the latest code
- Run `cargo clean` and rebuild
- Check Rust version (requires 1.80+)

## Next Steps After Verification

Once manual verification is complete and all checks pass:

1. Consider running the service for an extended period to verify stability
2. Monitor database size growth
3. Test error recovery by temporarily using invalid OAuth2 credentials
4. Proceed to Phase 2: Additional provider integration
