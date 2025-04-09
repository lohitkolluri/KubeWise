#!/bin/sh

# Check if Redis container exists and is running
if [ "$(docker ps -q -f name=redis)" ]; then
    echo "Redis container is already running"
else
    # Check if Redis container exists but is not running
    if [ "$(docker ps -aq -f name=redis)" ]; then
        echo "Starting existing Redis container..."
        docker start redis
    else
        echo "Creating and starting new Redis container..."
        docker run --name redis -p 6379:6379 -d redis:7
    fi
fi

echo "Redis is now available at localhost:6379"
