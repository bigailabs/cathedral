# Deploying Multi-File Projects with Custom Docker Images

For multi-file Python projects, build your own Docker image and deploy it to Basilica.

## Project Structure

```
my-project/
  app/
    __init__.py
    main.py
    routes/
      users.py
      items.py
  requirements.txt
  Dockerfile
  deploy.py
```

## Step 1: Create Your Dockerfile

```dockerfile
FROM python:3.11-slim

# Create non-root user first (required by Basilica)
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application and set ownership
COPY --chown=appuser:appuser app/ ./app/

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

## Step 2: Build and Push to Registry

```bash
# Build image
docker build -t ghcr.io/yourusername/my-api:latest .

# Push to GitHub Container Registry (or Docker Hub, ECR, etc.)
docker push ghcr.io/yourusername/my-api:latest
```

## Step 3: Deploy to Basilica

```python
from basilica import BasilicaClient

client = BasilicaClient()

deployment = client.deploy(
    name="my-api",
    image="ghcr.io/yourusername/my-api:latest",
    port=8000,
    ttl_seconds=3600,
)

print(f"Deployed: {deployment.url}")
```

## Complete Example Files

See the files in this directory:
- `app/` - Sample FastAPI application
- `requirements.txt` - Python dependencies
- `Dockerfile` - Container build instructions
- `deploy.py` - Basilica deployment script

## Notes

1. **Non-root execution**: Basilica runs containers as UID 1000. Your Dockerfile must:
   - Create the user before copying files: `RUN useradd -m -u 1000 appuser`
   - Set ownership when copying: `COPY --chown=appuser:appuser app/ ./app/`
   - Switch to that user: `USER appuser`

2. **Public registries**: Use public images or configure registry auth.

3. **Port**: Match the port in your Dockerfile CMD with the `port` parameter.
