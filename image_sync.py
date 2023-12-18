#!/usr/bin/env python3
"""
This script manages Docker images from an Azure registry. It supports
downloading and uploading Docker images based on specified criteria.

The script utilizes Docker and Azure CLI commands, Python's concurrent and 
threading features, and a custom ThreadPoolExecutor class for efficient 
multithreading.

Command-line arguments are used to specify the mode of operation (download or
upload), target repository, number of worker threads, logging level, and the
time frame for updating images.

It includes functions for:
- Fetching Azure and local Docker image checksums.
- Pulling and pushing images based on these checksums.
- Logging various stages of execution.

It handles interruptions gracefully and logs errors as they occur.

Usage:
Run the script with required arguments:
    python script_name.py --mode [download|upload] [additional arguments]

Requires:
- Docker client and Azure CLI configured on the execution environment.
- Proper access rights to the Azure Container Registry.
"""
import os
import sys
import subprocess
import json
import re
import concurrent.futures
import logging
import queue
import threading
import time
import argparse
from urllib.parse import urljoin
from datetime import datetime, timedelta, timezone
import docker
import requests
from dateutil import parser as dateutil_parser
from azure.identity import AzureCliCredential
from azure.mgmt.containerregistry import ContainerRegistryManagementClient

class ThreadPoolExecutor(concurrent.futures.ThreadPoolExecutor):
    """
    A custom implementation of ThreadPoolExecutor for managing a pool of threads.

    This class extends the ThreadPoolExecutor from the concurrent.futures
    module. It provides a framework for asynchronously executing callables
    using a pool of threads. The key feature of this custom implementation is
    its ability to dynamically adjust the number of threads in the pool based
    on the workload, ensuring efficient utilization of resources.

    The ThreadPoolExecutor is particularly useful for executing I/O-bound and
    high-level structurally blocking tasks, enabling the efficient execution of
    a large number of concurrent I/O operations.

    Attributes:
        _threads (set): A set to keep track of all the threads that have been
                        started.
        _max_workers (int): The maximum number of threads that can be active in
                            the pool at any given time.
        _thread_work_queue (queue.Queue): A queue that holds the tasks to be
                                          executed by the thread pool.
        _initializer (callable, optional): An optional initializer function
                                           that is called for each worker thread.
        _initargs (tuple): The arguments to pass to the initializer function.

    Methods:
        _adjust_thread_count: An internal method to adjust the number of
                              threads in the thread pool based on the 
        current workload and the maximum number of allowed workers.

    Example:
        # Create a ThreadPoolExecutor object with a maximum of 5 workers
        with ThreadPoolExecutor(max_workers=5) as executor:
            future = executor.submit(a_callable_function, arg1, arg2)
            # Do something with future.result() or future.exception()

    Note:
        - This custom ThreadPoolExecutor should be used when there is a need
          for dynamic adjustment of thread count.
        - It's important to ensure that the workload justifies the overhead of
          managing multiple threads.
    """
    def _adjust_thread_count(self):
        """
        Adjusts the thread count of the executor's thread pool and sets newly
        created threads as daemon threads.

        This method first calls the superclass's implementation to adjust the
        thread count according to the standard `ThreadPoolExecutor` behavior.
        It then iterates over any newly created threads (those added to the
        pool as a result of the superclass's adjustment) and sets them as daemon threads.

        Daemon threads are threads that run in the background and do not block
        the main program from exiting. When the main program exits, daemon
        threads are terminated immediately, which can be useful in contexts
        where these threads should not delay program termination.

        Note:
            - This method is an internal utility method and should not be
              called directly outside of the thread pool management context.
            - It modifies the private attribute `_threads`, which contains the
              set of all threads currently in the pool. This approach assumes
              the structure and behavior of the private attributes of
              `ThreadPoolExecutor` from the `concurrent.futures` module, which
              might change in future versions of Python.
        """
        current_thread_count = len(self._threads)

        # Adjust the thread count as per the original implementation
        super()._adjust_thread_count()

        # Set newly created threads as daemon threads
        for t in list(self._threads)[current_thread_count:]:
            if not t.daemon:
                t.daemon = True

# Set up argument parsing
parser = argparse.ArgumentParser(
    description='Manage Docker images from Azure registry.'
)
subparsers = parser.add_subparsers(dest='mode', required=True)
download_parser = subparsers.add_parser(
    'download', help='Download and update Docker images'
)
upload_parser = subparsers.add_parser(
    'upload', help='Upload Docker images'
)
parser.add_argument(
    '--repo', 
    help='Specify a specific repository or prefix to update',
    default=None
)
parser.add_argument(
    '--workers', 
    type=int,
    default=10,
    help='Number of worker threads to use'
)
parser.add_argument(
    '--loglevel', 
    default='INFO',
    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
    help='Set the logging level'
)
parser.add_argument(
    '--months', 
    type=int,
    default=12,
    help='Number of months to consider for updating images'
)
args = parser.parse_args()

# Determine if the --months argument was specified by the user
months_specified = args.months != 12  # Assuming 12 is the default value

# Set the CA bundle for SSL certificate verification
os.environ['REQUESTS_CA_BUNDLE'] = '/etc/ssl/certs/ca-certificates.crt'

# Set up logging based on the command-line argument
numeric_level = getattr(logging, args.loglevel.upper(), None)
if not isinstance(numeric_level, int):
    raise ValueError(f'Invalid log level: {args.loglevel}')
logging.basicConfig(level=numeric_level,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Azure registry name
AZURE_REGISTRY_NAME = 'mirantis.azurecr.io'

# Initialize Docker client
docker_client = docker.from_env()

# Initialize Azure credentials using Azure CLI credentials
credential = AzureCliCredential()

# Fetch subscription ID using Azure CLI
try:
    SUBSCRIPTION_ID = json.loads(subprocess.check_output(
        ["az", "account", "show", "--query", "id", "-o", "json"],
        text=True
    )).strip('"')
    logging.info("Successfully obtained subscription ID.")
except subprocess.CalledProcessError as subprocess_error:
    logging.error("Error executing Azure CLI command: %s", subprocess_error)
    SUBSCRIPTION_ID = None
except json.JSONDecodeError as json_error:
    logging.error("Error parsing JSON output: %s", json_error)
    SUBSCRIPTION_ID = None

# Check for subscription ID
if not SUBSCRIPTION_ID:
    raise ValueError("No subscription ID found. Please login to Azure CLI and "
                    "set a default subscription.")

# Initialize Azure Container Registry client
acr_client = ContainerRegistryManagementClient(credential, SUBSCRIPTION_ID)

# Specific repositories to always pull
SPECIFIC_REPOS = ["lcm/socat",
                  "openstack/extra/kubernetes-entrypoint",
                  "stacklight/configmap-reload",
                  "general/mariadb",
                  "stacklight/mysqld-exporter",
                  "openstack/extra/defaultbackend", 
                  "openstack/extra/coredns"]

# Cutoff date to not download all repos with approx number of days.
cutoff_date = datetime.now(timezone.utc) - timedelta(days=args.months * 30)
logging.info("Cutoff date: %s", cutoff_date)


def get_resource_group(registry_name):
    """
    Retrieves the Azure resource group for a given registry.

    Args:
    registry_name (str): The name of the Azure Container Registry.

    Returns:
    str or None: The name of the resource group if found, otherwise None.

    Executes an Azure CLI command to find the resource group. Logs the
    determined resource group or any errors encountered in the process.
    """
    logging.info("Trying to determine Azure resource group")
    try:
        result = subprocess.run(
            ["az", "acr", "show", "--name", registry_name,
             "--query", "resourceGroup", "-o", "tsv"],
            capture_output=True, text=True, check=True
        )
        resource_group = result.stdout.strip()
        logging.debug("Determined Azure resource group for %s: %s",
                      registry_name, resource_group)
        return resource_group
    except subprocess.CalledProcessError as resource_group_error:
        logging.error("Error getting resource group for %s: %s",
                      registry_name, resource_group_error)
        return None


def process_repository(registry_name, repo):
    """
    Processes a repository to fetch Azure image checksums and timestamps.

    Args:
    registry_name (str): The name of the registry holding the repo.
    repo (str): The name of the repository to process.

    Returns:
    dict: A dictionary mapping formatted image tags to their digest.

    Fetches manifest details for each repository from Azure. Parses timestamps
    and filters images based on specific repositories or cutoff dates.
    Logs details of each image processed, along with any errors.
    """
    azure_checksums = {}
    logging.info("Fetching manifest details for repository: %s", repo)
    try:
        manifest_result = subprocess.run(
            ["az", "acr", "repository", "show-manifests", "--name",
             registry_name, "--repository", repo, "--output", "json"],
            capture_output=True, text=True, check=True
        )
        manifests = json.loads(manifest_result.stdout)

        for manifest in manifests:
            # Check if the manifest has tags
            if "tags" in manifest and manifest["tags"]:
                tag = manifest["tags"][0]  # Assuming one tag per manifest
                # Skip tags that end with .sig
                if tag.endswith('.sig'):
                    continue
                digest = manifest["digest"]
                timestamp = manifest["timestamp"]

                try:
                    parsed_timestamp = dateutil_parser.parse(timestamp)
                    # If the timestamp is offset-naive
                    if parsed_timestamp.tzinfo is None:
                        # Assume UTC
                        parsed_timestamp = parsed_timestamp.replace(
                                                            tzinfo=timezone.utc)
                    if repo in SPECIFIC_REPOS or parsed_timestamp > cutoff_date:
                        formatted_tag = f"{repo}:{tag}"
                        azure_checksums[formatted_tag] = digest
                        logging.debug("Azure image: %s, Digest: %s",
                                      formatted_tag, digest)

                except ValueError as timestamp_error:
                    logging.error("Error parsing timestamp for %s:%s: %s",
                                  repo, tag, timestamp_error)

            else:
                logging.warning("No tags found for manifest with digest "
                                "%s in repository %s",
                                manifest.get('digest', 'unknown'), repo)

    except subprocess.CalledProcessError as registry_subprocess_error:
        logging.error("Error executing subprocess for %s: %s",
                      repo, registry_subprocess_error)
    except json.JSONDecodeError as registry_json_error:
        logging.error("Error parsing JSON for %s: %s",
                      repo, registry_json_error)
    except subprocess.TimeoutExpired as timeout_error:
        logging.error("Subprocess timed out for %s: %s", repo, timeout_error)

    return azure_checksums


def get_azure_images_checksums(registry_name, repo):
    """
    Fetches checksums of all images from Azure Container Registry.

    Args:
    registry_name (str): The name of the registry to process.
    repo (str): The name of the repository to filter on.

    Returns:
    dict: A dictionary mapping image tags in Azure to their checksums.

    Retrieves a list of repositories from Azure, filters them based on
    arguments, and then fetches their respective checksums using concurrent
    processing. Handles and logs errors during the fetching process and
    interruptions.
    """
    logging.info("Fetching Azure images")
    all_azure_checksums = {}
    try:
        logging.info("Fetching list of repositories from "
                     "Azure Container Registry")
        repos_result = subprocess.run(
            ["az", "acr", "repository", "list", "--name",
             registry_name, "--output", "json"],
            capture_output=True, text=True, check=True
        )
        all_repositories = json.loads(repos_result.stdout)
        logging.debug("Found repositories: %s", all_repositories)

        # Filter repositories based on the provided argument
        if repo:
            repositories = [
                r for r in all_repositories if r.startswith(repo)
            ]
            logging.debug("Filtered repositories (starting with '%s'): %s",
                          repo, repositories)

        else:
            repositories = all_repositories
            logging.debug("Processing all repositories.")

        try:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=args.workers) as executor:
                future_to_repo = {
                    executor.submit(process_repository,
                                    registry_name, repo): repo
                    for repo in repositories
                }
                for future in concurrent.futures.as_completed(future_to_repo):
                    repo = future_to_repo[future]
                    try:
                        azure_checksums = future.result()
                        all_azure_checksums.update(azure_checksums)
                    except subprocess.CalledProcessError as futures_error:
                        logging.error("Error processing repository %s: %s",
                                      repo, futures_error)
                    except json.JSONDecodeError as futures_json_error:
                        logging.error("Error parsing JSON for repo %s: %s",
                                      repo, futures_json_error)


        except KeyboardInterrupt:
            executor.shutdown(wait=False)

        logging.info("Completed fetching Azure images checksums")
    except subprocess.CalledProcessError as azure_subprocess_error:
        logging.error("Error executing Azure CLI command: %s",
                      azure_subprocess_error)
    except json.JSONDecodeError as azure_json_error:
        logging.error("Error parsing JSON from Azure CLI: %s",
                      azure_json_error)

    return all_azure_checksums


def get_remote_docker_images(registry_name):
    """
    Retrieves digests of all images from a specified Docker registry.

    Args:
    registry_name (str): The name of the Docker registry.

    Returns:
    dict: A dictionary mapping image tags to their digests.

    Gathers a list of repositories, then fetches tags and digests for each
    repository. Utilizes concurrent processing for efficiency. Handles
    interruptions and errors, logging them appropriately.
    """
    logging.info("Fetching remote images")
    remote_checksums = {}


    def get_next_page_url(response):
        """
        Extracts the next page URL from a paginated HTTP response.

        Args:
        response (requests.Response): The HTTP response object.

        Returns:
        str or None: The URL of the next page if available, otherwise None.

        Parses the 'Link' header in the response to find the URL for the next
        page. Converts relative URLs to absolute if necessary. Logs the URL of
        the next page if found.
        """
        link_header = response.headers.get('Link', None)
        if link_header:
            # Extract URL from <url>; rel="next"
            parts = link_header.split(';')
            if 'rel="next"' in parts[1]:
                next_page_url = parts[0].strip('<> ')
                # Convert relative URL to absolute URL
                next_page_url = urljoin(response.url, next_page_url)
                logging.debug("Next page URL: %s", next_page_url)
                return next_page_url
        return None


    def get_repositories():
        """
        Fetches a list of all repositories from a Docker registry.

        Returns:
        list: A list of repository names from the Docker registry.

        Makes GET requests to the registry's catalog endpoint, handling
        pagination via the 'get_next_page_url' function. Logs information and
        errors during the fetching process.
        """
        logging.info("Fetching list of repositories")
        repositories = []
        url = f"https://{registry_name}/v2/_catalog"
        while url:
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                data = response.json()
                repositories.extend(data.get('repositories', []))

                # Get the next page URL from Link header
                url = get_next_page_url(response)
            except requests.RequestException as fetch_error:
                logging.error("Error fetching repositories from %s: %s",
                              registry_name, fetch_error)
                break  # Exit the loop on error
        logging.debug("List of repositories: %s", repositories)

        return repositories


    def get_tags_for_repository(repository):
        """
        Retrieves all tags for a given Docker repository.

        Args:
        repository (str): The name of the Docker repository.

        Returns:
        list: A list of tags associated with the repository.

        Fetches tags by making GET requests to the Docker registry. Handles
        pagination if the registry provides a 'next' page link. Logs
        information and warnings about the fetching process and potential
        errors.
        """
        logging.info("Fetching tags for %s", repository)
        tags = []
        url = f"https://{registry_name}/v2/{repository}/tags/list"
        while url:
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                data = response.json()
                tags.extend(data.get('tags', []))

                # Check for the next page link
                url = data.get('next')
            except requests.RequestException as tags_error:
                logging.warning("Error fetching tags for repository %s: %s",
                                repository, tags_error)
                break  # Exit the loop on error
        logging.debug("List of tags: %s", tags)
        return tags


    def get_digest_for_tag(repository, tag):
        """
        Fetches the digest for a given tag in a Docker repository.

        Args:
        repository (str): The name of the Docker repository.
        tag (str): The tag of the Docker image in the repository.

        Returns:
        str or None: The digest of the image if found, otherwise None.

        Makes an HTTP HEAD request to the Docker registry and retrieves the
        'Docker-Content-Digest' header. Logs debug and error messages as
        appropriate.
        """
        logging.debug("Fetching digest for %s, tag: %s", repository, tag)
        url = f"https://{registry_name}/v2/{repository}/manifests/{tag}"
        headers = {
            'Accept': 'application/vnd.docker.distribution.manifest.v2+json'
        }
        try:
            response = requests.head(url, headers=headers, timeout=10)
            response.raise_for_status()
            return response.headers.get('Docker-Content-Digest')
        except requests.RequestException as digest_error:
            logging.error("Error fetching digest for image %s:%s: %s",
                          repository, tag, digest_error)
            return None


    def process_docker_repository(repository):
        """
        Processes a Docker repository by fetching tags and their digests.

        Args:
        repository (str): The name of the Docker repository to process.

        Populates the 'remote_checksums' dict with tag-digest pairs for
        each tag in the repository. Logs the tag and digest of each remote
        image processed.
        """
        tags = get_tags_for_repository(repository)
        for tag in tags:
            digest = get_digest_for_tag(repository, tag)
            if digest:
                formatted_tag = f"{repository}:{tag}"
                remote_checksums[formatted_tag] = digest
                logging.debug("Remote image: %s, Digest: %s",
                              formatted_tag, digest)


    repositories = get_repositories()

    # Using ThreadPoolExecutor to parallelize repository processing
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=args.workers) as executor:
            # Submitting each repository to the executor
            future_to_repo = {
                executor.submit(process_docker_repository, repo): repo
                for repo in repositories
            }

            # Iterating over the completed futures
            for future in concurrent.futures.as_completed(future_to_repo):
                repo = future_to_repo[future]
                try:
                    # This will also raise any exceptions caught in
                    # process_docker_repository
                    future.result()
                except concurrent.futures.TimeoutError as docker_timeout_error:
                    logging.error("Timeout error processing repository %s: %s",
                                  repo, docker_timeout_error)
                except RuntimeError as docker_runtime_error:
                    logging.error("Runtime error processing repository %s: %s",
                                  repo, docker_runtime_error)
    except KeyboardInterrupt:
        executor.shutdown(wait=False)

    logging.info("Completed fetching remote images checksums")
    return remote_checksums

# Function to fetch local Docker images - to be run in a separate thread
def fetch_docker_images():
    """
    Retrieves a list of all local Docker images using the Docker client.

    Returns:
        list: A list of fetched Docker images, or an empty list if an error
              occurs or the process is interrupted.

    Note:
    Logs errors or interruptions during the image fetching process.
    """
    while True:
        try:
            return docker_client.images.list()
        except KeyboardInterrupt:
            logging.info("Interrupted while fetching local Docker images.")
            return []  # Setting to an empty list
        except docker.errors.APIError as error:
            error_message = str(error)
            if ("500 Server Error" in error_message and
                "overlay2: invalid argument" in error_message):
                # Extract the SHA256 digest from the error message
                match = re.search(r'sha256:\b[0-9a-f]{64}\b', error_message)
                if match:
                    image_digest = match.group(0)
                    try:
                        # Delete the problematic image using Docker CLI
                        docker_client.images.remove(image=image_digest)
                        logging.info("Successfully deleted problematic image "
                                     "with digest %s", image_digest)
                    except docker.errors.ImageNotFound as fetch_not_found_error:
                        logging.error("Image with digest %s not found: %s",
                                      image_digest, fetch_not_found_error)
                        return []
                    except docker.errors.APIError as fetch_api_error:
                        logging.error("API error deleting Docker image with "
                                      "digest %s: %s", 
                                      image_digest, fetch_api_error)
                        return []
                else:
                    logging.error(
                        "SHA256 digest not found in the error message."
                    )
                    # If SHA256 digest couldn't be found, exit the loop
                    return []
            else:
                logging.error("Error fetching local Docker images: %s", error)
                return []


def get_local_docker_images(registry_name):
    """
    Fetches local Docker images from a local docker image cache and extracts
    their metadata.

    Args:
    registry_name (str): Name of the Docker registry to filter for.

    Returns:
    dict: A dictionary with image tags as keys and tuples of (digest, creation
    date) as values.

    Note:
    Logs the fetching process. Filters images based on the registry name.
    Handles parsing of image attributes like creation date and digest.
    """

    def thread_target(result_queue):
        """Wrapper function to execute fetch_docker_images and store result."""
        result = fetch_docker_images()
        result_queue.put(result)

    logging.info("Starting to fetch local Docker images. "
                 "This might take some time...")

    # Start the thread to fetch Docker images
    result_queue = queue.Queue()
    image_fetch_thread = threading.Thread(target=thread_target,
                                          args=(result_queue,))
    image_fetch_thread.start()

    # While the thread is alive, periodically log a message
    while image_fetch_thread.is_alive():
        logging.info("Still fetching local Docker images...")
        time.sleep(10)

    image_fetch_thread.join()
    all_images = result_queue.get()  # Retrieve the result from the queue
    logging.info("Finished fetching local Docker images.")

    try:
        local_checksums = {}
        # Filter images based on the registry name and format tags
        for img in all_images:
            if img.attrs['RepoDigests']:
                for tag in img.tags:
                    if registry_name in tag:
                        # Remove the registry URL part
                        formatted_local_tag = tag.replace(registry_name + "/",
                                                          "")
                        digest = img.attrs['RepoDigests'][0].split('@')[-1]
                        creation_date_str = img.attrs['Created']
                        try:
                            creation_date = dateutil_parser.parse(
                                                            creation_date_str)
                            # If the timestamp is offset-naive
                            if creation_date.tzinfo is None:
                                # Assume UTC
                                creation_date = creation_date.replace(
                                                            tzinfo=timezone.utc)
                        except ValueError as date_error:
                            logging.error("Error parsing creation date for "
                                          "%s: %s", tag, date_error)
                            continue
                        local_checksums[formatted_local_tag] = (digest,
                                                                creation_date)
                        logging.debug("Local image: %s, Digest: %s, Created: %s",
                                      formatted_local_tag,
                                      digest, creation_date)

        return local_checksums
    except ValueError as local_processing_error:
        logging.error("Error processing local Docker images: %s",
                      local_processing_error)
        return {}


def sync_image(action, tag, local_digest_tuple, checksums, registry_name):
    """
    Synchronizes a Docker image by either pulling or pushing, based on the 
    action specified. This is determined by comparing the local digest with 
    the provided checksums (either Azure or remote).

    Parameters:
    action (str): The action to perform - 'pull' or 'push'.
    tag (str): The tag of the Docker image.
    local_digest_tuple (tuple): A tuple containing the local digest.
    checksums (dict): A dictionary mapping image tags to their digests.
    registry_name (str): The name of the registry (Azure or other remote).

    Note:
    - The function logs various messages based on the operation's state and 
      result.
    - Assumes 'docker_client' is defined externally.
    - Skips images with complex tag structures (more than one colon).
    """
    if action == 'pull':
        local_digest = local_digest_tuple
        other_digest = checksums.get(tag, (None,))[0]
    elif action == 'push':
        local_digest = local_digest_tuple[0] if local_digest_tuple else 'none'
        other_digest = checksums.get(tag)
    else:
        raise ValueError(f"Unknown action: {action}")
    full_image_path = f"{registry_name}/{tag}"

    if tag.count(':') > 1:
        logging.warning("Skipping image with complex tag structure: %s", tag)
        return

    if local_digest != other_digest:
        logging.info("%s: Change detected - Local digest: %s, Other digest: %s",
                     tag, local_digest, other_digest)

        try:
            if action == 'pull':
                docker_client.images.pull(full_image_path)
                logging.info("Successfully pulled image: %s", full_image_path)
            elif action == 'push':
                response = docker_client.api.push(full_image_path,
                                                  stream=True, decode=True)
                for line in response:
                    logging.debug("%s: %s", tag, line)
                logging.info("%s: Successfully pushed image", tag)
            else:
                raise ValueError(f"Unknown action: {action}")
        except docker.errors.ImageNotFound as not_found_error:
            logging.error("Image not found %s: %s",
                          full_image_path, not_found_error)
        except docker.errors.APIError as image_api_error:
            logging.error("Docker API error %sing image %s: %s",
                          action, full_image_path, image_api_error)
    else:
        logging.debug("%s: No change detected", tag)


def format_time(seconds):
    """Converts time in seconds to a human-readable format of hours, minutes,
    and seconds."""
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds = seconds % 60
    return f"{hours}h:{minutes}m:{seconds}s"


def log_progress(total_tasks, completed_tasks, start_time, window_size=10):
    """
    Periodically logs the progress and estimated time remaining for a set of
    tasks being completed.

    This function runs in a loop, checking the number of completed tasks,
    calculating the percentage of total tasks completed, and estimating the
    time remaining based on a weighted moving average of recent task durations.
    The progress and estimated time remaining are logged every 10 seconds.
    The loop continues until all tasks are reported as finished.

    Args:
        total_tasks (int): The total number of tasks to track.
        completed_tasks (dict): A dictionary containing two keys:
            'count' (int): The number of tasks completed so far.
            'finished' (bool): A flag indicating whether all tasks are
                               completed.
        start_time (float): The start time of the task processing, as a
                            timestamp.
        window_size (int, optional): The number of most recent tasks to consider
                                     for calculating the weighted moving average
                                     of task durations. Defaults to 10.

    The function logs a progress update every 10 seconds in the format:
    "PROCESSED X/Y ITEMS (Z%), ESTIMATED TIME REMAINING: HH:MM:SS", where X is
    the number of completed tasks, Y is the total number of tasks, Z is the
    percentage of tasks completed, and HH:MM:SS is the estimated time remaining.

    After all tasks are completed, it logs a final message indicating 100%
    completion and the total time elapsed.
    """
    task_durations = []  # List to store the duration of the last few tasks

    while not completed_tasks['finished']:
        completed_count = completed_tasks['count']
        current_time = time.time()
        elapsed_time = current_time - start_time

        # Update task durations list
        if completed_count > 0:
            if len(task_durations) >= window_size:
                task_durations.pop(0)  # Remove oldest duration
            task_durations.append(elapsed_time / completed_count)

            # Calculate weighted moving average
            weighted_avg_duration = sum(task_durations) / len(task_durations)
            estimated_total_time = weighted_avg_duration * total_tasks
            estimated_time_remaining = estimated_total_time - elapsed_time
        else:
            estimated_time_remaining = float('inf')  # Infinite initially

        percentage = (completed_count / total_tasks) * 100
        formatted_time_remaining = format_time(estimated_time_remaining)
        logging.info("PROCESSED %d/%d ITEMS (%.2f%%), "
                     "ESTIMATED TIME REMAINING: %s",
                     completed_count, total_tasks, percentage,
                     formatted_time_remaining)

        time.sleep(10)

    # Final log to show 100% completion
    total_elapsed_time = time.time() - start_time
    formatted_total_elapsed_time = format_time(total_elapsed_time)
    logging.info("PROCESSED %d/%d ITEMS (100%%), TOTAL TIME ELAPSED: %s",
                 total_tasks, total_tasks, formatted_total_elapsed_time)


def process_images(action, registry_name, repo):
    """
    Process Docker images based on specified action ('pull' or 'push').

    This function handles the synchronization of Docker images between a local
    environment and an Azure registry. It supports two actions: 'pull' to
    download images from Azure to a local docker image cache, and 'push' to
    upload local images to a Docker registry. The synchronization process
    involves comparing image checksums to determine which images need to be
    transferred. The function also supports filtering images based
    on repository names and cutoff dates, primarily for the 'push' action.

    Args:
        action (str): The action to perform - either 'pull' or 'push'.
        registry_name (str): The name of the registry to process.
        repo (str): The name of the repository to filter on.

    Raises:
        AssertionError: If the action is not 'pull' or 'push'.
        KeyboardInterrupt: If the process is interrupted manually.

    Notes:
        - The function utilizes multithreading for efficient image processing.
        - It logs detailed information about the images being processed.
        - The 'push' action includes optional filters for specific repositories
          and images newer than a specified date.
    """
    assert action in ['pull', 'push'], "Action must be either 'pull' or 'push'."

    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_local_images = executor.submit(get_local_docker_images,
                                                  registry_name)
            if action == 'pull':
                future_remote_checksums = executor.submit(
                    get_azure_images_checksums, registry_name, repo
                )
            else:  # action == 'push'
                future_remote_checksums = executor.submit(
                    get_remote_docker_images, registry_name
                )

            local_checksums = future_local_images.result()
            remote_checksums = future_remote_checksums.result()
    except KeyboardInterrupt:
        executor.shutdown(wait=False)
        raise

    # Filter local images if necessary, only for 'push' action
    if action == 'push':
        # Filter local images based on the provided argument
        if repo:
            local_checksums = {
                tag: digest
                for tag, digest in local_checksums.items()
                if tag.startswith(repo)
            }
            logging.info("Filtered local images (starting with '%s'): %s",
                         repo, list(local_checksums.keys()))

        else:
            logging.debug("Processing all local images.")

        # Additional block for date filtering
        if cutoff_date:
            local_checksums = {
                tag: digest_tuple
                for tag, digest_tuple in local_checksums.items()
                if tag.split(':')[0] in SPECIFIC_REPOS or \
                    digest_tuple[1] >= cutoff_date
            }
            if months_specified:
                logging.info("Filtered local images (newer than %s and not "
                             "in SPECIFIC_REPOS): %s", cutoff_date,
                             list(local_checksums.keys()))

            else:
                logging.debug("Filtered local images (newer than %s and not "
                              "in SPECIFIC_REPOS): %s", cutoff_date,
                              list(local_checksums.keys()))

    logging.info("Comparing images")
    try:
        # Set max_workers based on the action
        max_workers = 2 if action == 'push' else args.workers
        completed_tasks = {'count': 0, 'finished': False}

        with concurrent.futures.ThreadPoolExecutor(
                max_workers) as executor:
            # Start the progress logging in a separate thread
            start_time = time.time()
            if action == 'push':
                total_tasks = len(local_checksums)
            else:
                total_tasks = len(remote_checksums)

            log_thread = threading.Thread(target=log_progress,
                                          args=(total_tasks, completed_tasks,
                                                start_time))
            log_thread.start()

            if action == 'pull':
                future_to_image = {
                    executor.submit(
                        sync_image, 'pull', remote_tag, remote_digest,
                        local_checksums, AZURE_REGISTRY_NAME
                    ): remote_tag
                    for remote_tag, remote_digest in remote_checksums.items()
                }
            else:  # action == 'push'
                future_to_image = {
                    executor.submit(
                        sync_image, 'push', local_tag, local_digest,
                        remote_checksums, AZURE_REGISTRY_NAME
                    ): local_tag
                    for local_tag, local_digest in local_checksums.items()
                }

            for future in concurrent.futures.as_completed(future_to_image):
                tag = future_to_image[future]
                future.result()
                completed_tasks['count'] += 1  # Increment the count of tasks

        completed_tasks['finished'] = True  # Signal that all tasks are complete
        log_thread.join()  # Wait for the logging thread to finish

    except KeyboardInterrupt:
        executor.shutdown(wait=False)

# Run the main function and handle KeyboardInterrupt
if __name__ == "__main__":
    logging.info("Starting process.")

    try:
        if args.mode == 'download':
            logging.info("Running in download mode.")
            azure_resource_group = get_resource_group(AZURE_REGISTRY_NAME)
            process_images("pull", AZURE_REGISTRY_NAME, args.repo)
            logging.info("Image download process complete.")
        elif args.mode == 'upload':
            logging.info("Running in upload mode")
            process_images("push", AZURE_REGISTRY_NAME, args.repo)
            logging.info("Image upload process complete.")
        else:
            raise ValueError(f"Unknown mode: {args.mode}")
    except KeyboardInterrupt:
        logging.info("Process interrupted by user.")
        sys.exit(1)
