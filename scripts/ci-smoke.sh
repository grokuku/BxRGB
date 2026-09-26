#!/bin/bash
set -euo pipefail

case "${1:-}" in
  config)
    echo "Validating docker-compose.yml..."
    docker compose config -q
    ;;
  smoke)
    echo "Starting smoke test..."
    docker compose up -d
    
    echo "Waiting for service to be ready..."
    # Wait up to 15 seconds for the /api/status to return "running"
    for i in {1..15}; do
      if curl -s http://localhost:8080/api/status | grep -q "running"; then
        echo "Service is running!"
        docker compose down
        exit 0
      fi
      echo "Attempt $i/15: Service not ready yet..."
      sleep 1
    done
    
    echo "Smoke test failed: Service did not reach 'running' state in time."
    docker compose logs
    docker compose down
    exit 1
    ;;
  *)
    echo "Usage: $0 {config|smoke}"
    exit 1
    ;;
esac
