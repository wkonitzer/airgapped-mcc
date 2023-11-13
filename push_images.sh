#!/bin/bash

# Variables
REGISTRY="mirantis.azurecr.io"
PARALLEL_UPLOADS=5  # Number of parallel uploads
LOG_FILE="/tmp/push_images.log"

# Function to push the image
push_image() {
  local image=$1
  echo "Pushing $image"
  if docker push "$image" 2>>"$LOG_FILE"; then
    echo "Successfully pushed $image" >>"$LOG_FILE"
  else
    echo "Failed to push $image" >>"$LOG_FILE"
  fi
}

# Initialize counter
counter=0

# Clear the log file at the start of the script
> "$LOG_FILE"

# List all images from the mirantis.azurecr.io registry and push them to the local registry
docker image ls --format "{{.Repository}}:{{.Tag}}" | grep "^$REGISTRY" | while read -r image; do
  # Call push_image in background
  push_image "$image" &

  ((counter++))

  # Wait for all background jobs to finish if the counter reaches the limit
  if (( counter >= PARALLEL_UPLOADS )); then
    wait
    counter=0
  fi
done

# Wait for any remaining background jobs to complete
wait

echo "All images have been pushed to the local registry."
