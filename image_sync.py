#!/usr/bin/env python3
# version 0.1
import os
import subprocess
import docker
import json
import concurrent.futures
import logging
import threading
import time
import argparse
import requests
from urllib.parse import urljoin
from datetime import datetime, timedelta, timezone
from dateutil import parser as dateutil_parser
from azure.identity import AzureCliCredential
from azure.mgmt.containerregistry import ContainerRegistryManagementClient

class ThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    def _adjust_thread_count(self):
        for _ in range(len(self._threads), self._max_workers):
            t = threading.Thread(target=self._worker, args=(
                self._thread_work_queue,
                self._initializer,
                self._initargs,
            ))
            t.daemon = True  # Set daemon flag
            t.start()
            self._threads.add(t)

# Set up argument parsing
parser = argparse.ArgumentParser(description='Manage Docker images from Azure registry.')
subparsers = parser.add_subparsers(dest='mode', required=True)
download_parser = subparsers.add_parser('download', help='Download and update Docker images')
upload_parser = subparsers.add_parser('upload', help='Upload Docker images')
parser.add_argument('--repo', help='Specify a specific repository or prefix to update', default=None)
parser.add_argument('--workers', type=int, default=10, help='Number of worker threads to use')
parser.add_argument('--loglevel', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], help='Set the logging level')
parser.add_argument('--months', type=int, default=12, help='Number of months to consider for updating images')
args = parser.parse_args()

# Determine if the --months argument was specified by the user
months_specified = args.months != 12  # Assuming 12 is the default value

# Set the CA bundle for SSL certificate verification
os.environ['REQUESTS_CA_BUNDLE'] = '/etc/ssl/certs/ca-certificates.crt'

# Set up logging based on the command-line argument
numeric_level = getattr(logging, args.loglevel.upper(), None)
if not isinstance(numeric_level, int):
    raise ValueError('Invalid log level: %s' % args.loglevel)
logging.basicConfig(level=numeric_level, format='%(asctime)s - %(levelname)s - %(message)s')

# Azure registry name
azure_registry_name = 'mirantis.azurecr.io'

# Initialize Docker client
docker_client = docker.from_env()

# Initialize Azure credentials using Azure CLI credentials
credential = AzureCliCredential()

# Fetch subscription ID using Azure CLI
try:
    subscription_id = json.loads(subprocess.check_output(
        ["az", "account", "show", "--query", "id", "-o", "json"],
        text=True
    )).strip('"')
    logging.info("Successfully obtained subscription ID.")
except Exception as e:
    logging.error(f"Error obtaining subscription ID: {e}")
    subscription_id = None

# Check for subscription ID
if not subscription_id:
    raise Exception("No subscription ID found. Please login to Azure CLI and set a default subscription.")

# Initialize Azure Container Registry client
acr_client = ContainerRegistryManagementClient(credential, subscription_id)

# Specific repositories to always pull
SPECIFIC_REPOS = ["lcm/socat", "openstack/extra/kubernetes-entrypoint", "stacklight/configmap-reload", 
                  "general/mariadb", "stacklight/mysqld-exporter", "openstack/extra/defaultbackend", 
                  "openstack/extra/coredns"]

# Cutoff date to not download all repos
cutoff_date = datetime.now(timezone.utc) - timedelta(days=args.months * 30)  # Approximate the number of days
logging.info(f"Cutoff date: {cutoff_date}")

# Function to get Azure resource group
def get_resource_group(registry_name):
    logging.info("Trying to determine Azure resource group")
    try:
        result = subprocess.run(
            ["az", "acr", "show", "--name", registry_name, "--query", "resourceGroup", "-o", "tsv"],
            capture_output=True, text=True, check=True
        )
        resource_group = result.stdout.strip()
        logging.debug(f"Determined Azure resource group for {registry_name}: {resource_group}")
        return resource_group
    except subprocess.CalledProcessError as e:
        logging.error(f"Error getting resource group for {registry_name}: {e}")
        return None

def process_repository(repo):
    azure_checksums = {}
    logging.info(f"Fetching manifest details for repository: {repo}")
    try:
        manifest_result = subprocess.run(
            ["az", "acr", "repository", "show-manifests", "--name", azure_registry_name, "--repository", repo, "--output", "json"],
            capture_output=True, text=True, check=True
        )
        manifests = json.loads(manifest_result.stdout)

        for manifest in manifests:
            # Check if the manifest has tags
            if "tags" in manifest and manifest["tags"]:
                tag = manifest["tags"][0]  # Assuming one tag per manifest
                digest = manifest["digest"]
                timestamp = manifest["timestamp"]

                try:
                    parsed_timestamp = dateutil_parser.parse(timestamp)
                    if parsed_timestamp.tzinfo is None:  # If the timestamp is offset-naive
                        parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)  # Assume UTC
                    if repo in SPECIFIC_REPOS or parsed_timestamp > cutoff_date:
                        formatted_tag = f"{repo}:{tag}"
                        azure_checksums[formatted_tag] = digest
                        logging.debug(f"Azure image: {formatted_tag}, Digest: {digest}")
                except ValueError as e:
                    logging.error(f"Error parsing timestamp for {repo}:{tag}: {e}")
            else:
                logging.warning(f"No tags found for manifest with digest {manifest.get('digest', 'unknown')} in repository {repo}")
    except Exception as e:
        logging.error(f"Error fetching manifest details for {repo}: {e}")

    return azure_checksums

def get_azure_images_checksums():
    logging.info("Fetching Azure images")
    all_azure_checksums = {}
    try:
        logging.info("Fetching list of repositories from Azure Container Registry")
        repos_result = subprocess.run(
            ["az", "acr", "repository", "list", "--name", azure_registry_name, "--output", "json"],
            capture_output=True, text=True, check=True
        )
        all_repositories = json.loads(repos_result.stdout)
        logging.debug(f"Found repositories: {all_repositories}")

        # Filter repositories based on the provided argument
        if args.repo:
            repositories = [repo for repo in all_repositories if repo.startswith(args.repo)]
            logging.debug(f"Filtered repositories (starting with '{args.repo}'): {repositories}")
        else:
            repositories = all_repositories
            logging.debug(f"Processing all repositories.")

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
                future_to_repo = {executor.submit(process_repository, repo): repo for repo in repositories}
                for future in concurrent.futures.as_completed(future_to_repo):
                    repo = future_to_repo[future]
                    try:
                        azure_checksums = future.result()
                        all_azure_checksums.update(azure_checksums)
                    except Exception as e:
                        logging.error(f"Error processing repository {repo}: {e}")
        except KeyboardInterrupt:
            executor.shutdown(wait=False)

        logging.info("Completed fetching Azure images checksums")
    except Exception as e:
        logging.error(f"Error fetching Azure images using Azure CLI: {e}")

    return all_azure_checksums

# Function to query local docker registry
def get_remote_docker_images(registry_name):
    logging.info("Fetching remote images")
    remote_checksums = {}

    # Function to extract link header
    def get_next_page_url(response):
        link_header = response.headers.get('Link', None)
        if link_header:
            # Extract URL from <url>; rel="next"
            parts = link_header.split(';')
            if 'rel="next"' in parts[1]:
                next_page_url = parts[0].strip('<> ')
                # Convert relative URL to absolute URL
                next_page_url = urljoin(response.url, next_page_url)
                logging.debug(f"Next page URL: {next_page_url}")
                return next_page_url
        return None

    # Function to get repositories from the remote registry
    def get_repositories():
        logging.info("Fetching list of repositories")
        repositories = []
        url = f"https://{registry_name}/v2/_catalog"
        while url:
            try:
                response = requests.get(url)
                response.raise_for_status()
                data = response.json()
                repositories.extend(data.get('repositories', []))

                # Get the next page URL from Link header
                url = get_next_page_url(response)
            except requests.RequestException as e:
                logging.error(f"Error fetching repositories from {registry_name}: {e}")
                break  # Exit the loop on error
        logging.debug(f"List of repositories: {repositories}")

        return repositories

    # Function to get tags for a specific repository
    def get_tags_for_repository(repository):
        logging.info(f"Fetching tags for {repository}")
        tags = []
        url = f"https://{registry_name}/v2/{repository}/tags/list"
        while url:
            try:
                response = requests.get(url)
                response.raise_for_status()
                data = response.json()
                tags.extend(data.get('tags', []))

                # Check for the next page link
                url = data.get('next')
            except requests.RequestException as e:
                logging.warning(f"Error fetching tags for repository {repository}: {e}")
                break  # Exit the loop on error
        logging.debug(f"List of tags: {tags}")
        return tags

    # Function to get the digest for a specific image tag
    def get_digest_for_tag(repository, tag):
        logging.debug(f"Fetching digest for {repository}, tag: {tag}")
        url = f"https://{registry_name}/v2/{repository}/manifests/{tag}"
        headers = {'Accept': 'application/vnd.docker.distribution.manifest.v2+json'}
        try:
            response = requests.head(url, headers=headers)
            response.raise_for_status()
            return response.headers.get('Docker-Content-Digest')
        except requests.RequestException as e:
            logging.error(f"Error fetching digest for image {repository}:{tag}: {e}")
            return None

    # Function to process each repository
    def process_repository(repository):
        tags = get_tags_for_repository(repository)
        for tag in tags:
            digest = get_digest_for_tag(repository, tag)
            if digest:
                formatted_tag = f"{repository}:{tag}"
                remote_checksums[formatted_tag] = digest
                logging.debug(f"Remote image: {formatted_tag}, Digest: {digest}")

    repositories = get_repositories()

    # Using ThreadPoolExecutor to parallelize repository processing
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            # Submitting each repository to the executor
            future_to_repo = {executor.submit(process_repository, repo): repo for repo in repositories}

            # Iterating over the completed futures
            for future in concurrent.futures.as_completed(future_to_repo):
                repo = future_to_repo[future]
                try:
                    future.result()  # This will also raise any exceptions caught in process_repository
                except Exception as e:
                    logging.error(f"Error processing repository {repo}: {e}")
    except KeyboardInterrupt:
        executor.shutdown(wait=False)                

    logging.info("Completed fetching remote images checksums")
    return remote_checksums

# Function to actually fetch Docker images - to be run in a separate thread
def fetch_docker_images():
    global all_images
    try:
        all_images = docker_client.images.list()
    except KeyboardInterrupt:
        logging.info("Interrupted while fetching local Docker images.")
        all_images = []  # Setting to an empty list
    except Exception as e:
        logging.error(f"Error fetching local Docker images: {e}")
        all_images = []

# Function to filter local Docker images by registry
def get_local_docker_images(registry_name):
    logging.info("Starting to fetch local Docker images. This might take some time...")

    # Start the thread to fetch Docker images
    image_fetch_thread = threading.Thread(target=fetch_docker_images)
    image_fetch_thread.start()

    # While the thread is alive, periodically log a message
    while image_fetch_thread.is_alive():
        logging.info("Still fetching local Docker images...")
        time.sleep(10)

    image_fetch_thread.join()
    logging.info("Finished fetching local Docker images.")

    try:
        local_checksums = {}
        # Filter images based on the registry name and format tags
        for img in all_images:
            if img.attrs['RepoDigests']:
                for tag in img.tags:
                    if registry_name in tag:
                        # Remove the registry URL part
                        formatted_local_tag = tag.replace(registry_name + "/", "")
                        digest = img.attrs['RepoDigests'][0].split('@')[-1]
                        creation_date_str = img.attrs['Created']
                        try:
                            creation_date = dateutil_parser.parse(creation_date_str)
                            if creation_date.tzinfo is None:  # If the timestamp is offset-naive
                                creation_date = creation_date.replace(tzinfo=timezone.utc)  # Assume UTC
                        except ValueError as e:
                            logging.error(f"Error parsing creation date for {tag}: {e}")
                            continue                      
                        local_checksums[formatted_local_tag] = (digest, creation_date)
                        logging.debug(f"Local image: {formatted_local_tag}, Digest: {digest}, Created: {creation_date}")

        return local_checksums
    except Exception as e:
        logging.error(f"Error processing local Docker images: {e}")
        return {}

def pull_image(azure_tag, azure_digest, local_checksums):
    # Retrieve the local digest tuple and extract just the digest part
    local_digest_tuple = local_checksums.get(azure_tag)
    local_digest = local_digest_tuple[0] if local_digest_tuple else None

    if azure_tag.count(':') > 1:
        logging.warning(f"Skipping image with complex tag structure: {azure_tag}")
        return

    if local_digest != azure_digest:
        full_image_path = f"{azure_registry_name}/{azure_tag}"
        logging.info(f"Change detected - Local digest: {local_digest}, Azure digest: {azure_digest} for image: {full_image_path}")
        try:
            docker_client.images.pull(full_image_path)
            logging.info(f"Successfully pulled image: {full_image_path}")
        except Exception as e:
            logging.error(f"Error pulling image {full_image_path}: {e}")
    else:
        logging.debug(f"No change detected for image: {azure_tag}")

def run_docker_push(full_image_path):
    with subprocess.Popen(["docker", "push", full_image_path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
        for line in proc.stdout:
            logging.info(line.strip())

def push_image(local_tag, local_digest_tuple, remote_checksums):
    # Extract just the digest part from the tuple
    local_digest = local_digest_tuple[0]    
    remote_digest = remote_checksums.get(local_tag)

    if local_tag.count(':') > 1:
        logging.warning(f"Skipping image with complex tag structure: {local_tag}")
        return

    if local_digest != remote_digest:
        full_image_path = f"{azure_registry_name}/{local_tag}"
        logging.info(f"{local_tag}: Change detected - Local digest: {local_digest}, Remote digest: {remote_digest}")
        try:
            response = docker_client.api.push(full_image_path, stream=True, decode=True)
            for line in response:
                logging.debug(f"{local_tag}: {line}")
            logging.info(f"{local_tag}: Successfully pushed image")
        except Exception as e:
            logging.error(f"{local_tag}: Error pushing image: {e}")
    else:
        logging.debug(f"{local_tag}: No change detected")

# Compare and pull images
def update_images():
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_local_images = executor.submit(get_local_docker_images, azure_registry_name)
            future_azure_checksums = executor.submit(get_azure_images_checksums)

            local_checksums = future_local_images.result()
            azure_checksums = future_azure_checksums.result()
    except KeyboardInterrupt:
        executor.shutdown(wait=False)

    logging.info("Comparing images") 
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_image = {
                executor.submit(pull_image, azure_tag, azure_digest, local_checksums): azure_tag
                for azure_tag, azure_digest in azure_checksums.items()
            }
            for future in concurrent.futures.as_completed(future_to_image):
                azure_tag = future_to_image[future]
                try:
                    # This will also raise any exceptions caught by pull_image
                    future.result()
                except Exception as e:
                    logging.error(f"Error in pulling image {azure_tag}: {e}")
    except KeyboardInterrupt:
        executor.shutdown(wait=False)

# Compare and push images
def upload_images():
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_local_images = executor.submit(get_local_docker_images, azure_registry_name)
            future_remote_checksums = executor.submit(get_remote_docker_images, azure_registry_name)

            local_checksums = future_local_images.result()
            remote_checksums = future_remote_checksums.result()
    except KeyboardInterrupt:
        executor.shutdown(wait=False)

    # Filter local images based on the provided argument
    if args.repo:
        local_checksums = {tag: digest for tag, digest in local_checksums.items() if tag.startswith(args.repo)}
        logging.info(f"Filtered local images (starting with '{args.repo}'): {list(local_checksums.keys())}")
    else:
        logging.debug("Processing all local images.")

    # Additional block for date filtering
    if cutoff_date:
        local_checksums = {
            tag: digest_tuple
            for tag, digest_tuple in local_checksums.items()
            if tag.split(':')[0] in SPECIFIC_REPOS or digest_tuple[1] >= cutoff_date
        }
        if months_specified:    
            logging.info(f"Filtered local images (newer than {cutoff_date} and not in SPECIFIC_REPOS): {list(local_checksums.keys())}")
        else:
            logging.debug(f"Filtered local images (newer than {cutoff_date} and not in SPECIFIC_REPOS): {list(local_checksums.keys())}")


    logging.info("Comparing images") 
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            future_to_image = {
                executor.submit(push_image, local_tag, local_digest, remote_checksums): local_tag
                for local_tag, local_digest in local_checksums.items()
            }
            for future in concurrent.futures.as_completed(future_to_image):
                local_tag = future_to_image[future]
                try:
                    # This will also raise any exceptions caught by push_image
                    future.result()
                except Exception as e:
                    logging.error(f"Error in pushing image {local_tag}: {e}")
    except KeyboardInterrupt:
        executor.shutdown(wait=False)

# Run the main function and handle KeyboardInterrupt
if __name__ == "__main__":
    logging.info("Starting process.")

    try:
        if args.mode == 'download':
            logging.info("Running in download mode.")
            azure_resource_group = get_resource_group(azure_registry_name)
            update_images()
            logging.info("Image download process complete.")
        elif args.mode == 'upload':
            logging.info("Running in upload mode")
            upload_images()
            logging.info("Image upload process complete.")
        else:
            raise ValueError(f"Unknown mode: {args.mode}")
    except KeyboardInterrupt:
        logging.info("Process interrupted by user.")
    except Exception as e:
        logging.error(f"An error occurred: {e}")