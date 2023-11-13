#!/bin/bash

# Variables
REGISTRY_NAME="mirantis"  # Replace with your registry name
PARALLEL_DOWNLOADS=5      # Number of parallel downloads

# Login to ACR
#az acr login --name $REGISTRY_NAME

# Get list of repositories in the registry
REPOSITORIES=$(az acr repository list --name $REGISTRY_NAME --output tsv)

# Function to pull docker images
pull_image() {
    IMAGE_NAME="$1"
    TIMESTAMP="$2"
    echo "Pulling $IMAGE_NAME created on $TIMESTAMP..."
    docker pull $IMAGE_NAME
}

# Initialize counter
counter=0

# Loop through each repository and fetch manifest details
for REPO in $REPOSITORIES; do
    # Get manifest details for the repository
    MANIFESTS=$(az acr repository show-manifests --name $REGISTRY_NAME --repository $REPO --output json)
    
    # Loop through the manifests and check the creation date
    for ROW in $(echo "${MANIFESTS}" | jq -r '.[] | @base64'); do
        _jq() {
          echo ${ROW} | base64 --decode | jq -r ${1}
        }

        TIMESTAMP=$(_jq '.timestamp')
        TAG=$(_jq '.tags[0]')  # Assuming one tag per manifest
        YEAR=$(echo $TIMESTAMP | cut -d '-' -f 1)

        # Check if the repository is lcm/socat or the year is greater or equal to 2023
        if [[ $REPO == "lcm/socat" ]] || [[ $YEAR -ge 2023 ]]; then
            IMAGE_NAME="$REGISTRY_NAME.azurecr.io/$REPO:$TAG"

            # Call pull_image in background
            pull_image "$IMAGE_NAME" "$TIMESTAMP" &

            # Increment counter
            ((counter++))

            # Wait for all background jobs to finish if the counter reaches the limit
            if (( counter == PARALLEL_DOWNLOADS )); then
                wait
                counter=0
            fi
        fi
    done
done

# Wait for any remaining background jobs to complete
wait
