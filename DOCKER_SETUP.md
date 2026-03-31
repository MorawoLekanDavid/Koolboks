# Docker Setup Guide for Koolbuy Chatbot

## Prerequisites
- Docker Desktop installed ([Download](https://www.docker.com/products/docker-desktop))
- Docker Compose (comes with Docker Desktop)
- Groq API key (get from [console.groq.com](https://console.groq.com))

## Quick Start

### 1. **Setup Environment Variables**
```bash
# Copy the example env file and add your Groq API key
cp .env.example .env

# Edit .env and set your GROQ_API_KEY
# GROQ_API_KEY=your_actual_api_key_here
```

### 2. **Build and Run with Docker Compose**
```bash
# Build the Docker image
docker-compose build

# Start the services (FastAPI + Redis) - now runs on port 8001
docker-compose up -d

# View logs
docker-compose logs -f api

# Stop the services
docker-compose down
```

### 3. **Test the API**
```bash
# Health check (now on port 8001)
curl http://localhost:8001/health

# Chat endpoint (example)
curl -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "test-session",
    "message": "__welcome__",
    "user_name": "John"
  }'
```

## Docker Commands Reference

### Build
```bash
# Build with a specific tag
docker-compose build --no-cache

# Build only the API service
docker build -t koolbuy-api:latest .
```

### Run
```bash
# Run in foreground (see logs)
docker-compose up

# Run in background
docker-compose up -d

# Run with specific service
docker-compose up api
docker-compose up redis
```

### Debug
```bash
# View logs
docker-compose logs -f api        # API logs
docker-compose logs -f redis      # Redis logs
docker-compose logs -f            # All logs

# Execute commands in running container
docker-compose exec api bash      # Shell into API container
docker-compose exec redis redis-cli  # Redis CLI

# Inspect container
docker-compose ps                 # List containers
docker-compose stats              # Container resource usage
```

### Cleanup
```bash
# Stop and remove containers
docker-compose down

# Remove volumes (Redis data)
docker-compose down -v

# Remove images
docker image rm koolbuy-api:latest
docker image rm redis:7-alpine
```

## File Persistence

Your data files are persisted via Docker volumes:
- `leads.csv` - Lead data saved to host machine
- `products.csv` - Product catalog
- `knowledge_base.txt` - AI knowledge base
- `system_prompt.txt` - AI system prompt
- Redis data in named volume `redis_data`

## WhatsApp Integration Setup

When you're ready to connect to WhatsApp, you can add a webhook service:

### Option 1: Run WhatsApp Webhook in Same Docker Network
```yaml
# Add to docker-compose.yml
services:
  whatsapp-webhook:
    build:
      context: ./whatsapp
      dockerfile: Dockerfile
    container_name: koolbuy-whatsapp
    environment:
      - API_URL=http://api:8000
      - WEBHOOK_TOKEN=${WEBHOOK_TOKEN}
    depends_on:
      - api
    ports:
      - "8001:8001"
    networks:
      - koolbuy-network
```

### Option 2: Expose API Publicly with Ngrok
```bash
# In a separate terminal, expose your local API
ngrok http 8000

# Use the ngrok URL as your WhatsApp webhook URL
# https://your-ngrok-url.ngrok.io/chat
```

## Deployment (Production)

### Using Docker Hub
```bash
# Tag your image
docker tag koolbuy-api:latest yourusername/koolbuy-api:latest

# Push to Docker Hub
docker login
docker push yourusername/koolbuy-api:latest
```

### Using Cloud Platforms

**Railway.app** (Recommended - Simple)
```bash
railway link            # Connect to your Railway project
railway up              # Deploy
```

**AWS ECS**
```bash
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin 123456789.dkr.ecr.us-east-1.amazonaws.com

docker tag koolbuy-api:latest 123456789.dkr.ecr.us-east-1.amazonaws.com/koolbuy-api:latest
docker push 123456789.dkr.ecr.us-east-1.amazonaws.com/koolbuy-api:latest
```

**Heroku** (with docker.io support)
```bash
heroku login
heroku container:login
heroku create koolbuy-api
heroku container:push web --app koolbuy-api
heroku container:release web --app koolbuy-api
```

## Troubleshooting

### Redis Connection Error
```
Error: Connection refused
Solution: Ensure Redis is running - docker-compose up redis
```

### Port Already in Use
```bash
# Change ports in docker-compose.yml
# Or kill the process using the port
lsof -i :8000
kill -9 <PID>
```

### API Can't Connect to Redis
```bash
# Check network connectivity
docker-compose exec api ping redis

# Check redis is healthy
docker-compose exec redis redis-cli ping
```

### Environment Variables Not Loaded
```bash
# Ensure .env file exists in project root
# Check docker-compose.yml has correct env_file or environment section
# Rebuild: docker-compose build --no-cache
```

## Next Steps

1. **Test locally first** - Run `docker-compose up` and test all endpoints
2. **Add WhatsApp webhook** - Create webhook service to integrate with WhatsApp Business API
3. **Deploy to production** - Use Railway, AWS, or Heroku
4. **Monitor logs** - Set up centralized logging (e.g., ELK stack)
5. **Add CI/CD** - GitHub Actions to auto-build and push on commits
