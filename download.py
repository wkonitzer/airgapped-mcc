#!/usr/bin/env python3
"""
Script to manage and download binary images from the Mirantis CDN.

This script navigates through specified URLs from the Mirantis CDN and 
downloads relevant binary images. It employs a headless browser for web
scraping and downloads files based on specific criteria such as URL patterns,
file creation dates, and size checks. The script supports concurrent downloads, 
logging, and has customizability options via command-line arguments.

Usage:
Run the script with optional command-line arguments to specify the number of 
worker threads, logging level, time range for updating images, and specific 
repository or prefix to download.

Features:
- Concurrent file downloading using ThreadPoolExecutor.
- Command-line argument parsing for script customization.
- Dynamic URL processing based on presence of specified prefixes and file 
  creation dates.
- Error handling and detailed logging for debugging and monitoring.

Requirements:
- Python 3
- External libraries: requests, bs4 (BeautifulSoup), selenium,
                      concurrent.futures, dateutil

Example:
`python3 script_name.py --workers 5 --loglevel INFO --months 12 --repo some_repo`
"""
import os
import sys
from urllib.parse import urljoin, urlparse
import concurrent.futures
from datetime import datetime, timedelta, timezone
import argparse
import logging
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException, WebDriverException
from dateutil import parser as dateutil_parser

# Set up argument parsing
parser = argparse.ArgumentParser(
                    description='Manage binary images from Mirantis CDN.')
parser.add_argument('--workers', type=int, default=5,
                    help='Number of worker threads to use')
parser.add_argument('--loglevel', default='INFO',
                    choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                    help='Set the logging level')
parser.add_argument('--months', type=int, default=12,
                    help='Number of months to consider for updating images')
parser.add_argument('--repo',
                    help='Specify a specific repository or prefix to download',
                    default=None)
parser.add_argument('--output_dir', type=str, default='/images/binaries',
                    help='The directory where the output should be stored')
args = parser.parse_args()

# Set up logging based on the command-line argument
numeric_level = getattr(logging, args.loglevel.upper(), None)
if not isinstance(numeric_level, int):
    raise ValueError(f'Invalid log level: {args.loglevel}')
logging.basicConfig(level=numeric_level,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Cutoff date to not download all repos, approx num of days
cutoff_date = datetime.now(timezone.utc) - timedelta(days=args.months * 30)
logging.info("Cutoff date: %s", cutoff_date)

# Determine if the --months argument was specified by the user
months_specified = args.months != 12  # Assuming 12 is the default value

BASE_URL = "https://binary.mirantis.com/"
if args.repo:
    BASE_URL = f"{BASE_URL}?prefix={args.repo}"

output_dir = args.output_dir
processed_urls = set()

# List of repos to ignore the date check
ignore_date_check_prefixes = ["stacklight/helm/",
                              "bm/stub"]

# Configure the WebDriver
options = webdriver.ChromeOptions()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 10)


def download_file(url, local_path):
    """
    Downloads a file from a specified URL to a local path.

    This function first checks if the file at the URL exists and whether a file 
    at the local path already exists with the same size. If the file exists and 
    the sizes match, the download is skipped. Otherwise, the file is downloaded 
    and saved to the specified local path.

    Parameters:
    url (str): The URL of the file to download.
    local_path (str): The local file path where the downloaded file will be
                      saved.

    Returns:
    None: The function returns None. It either successfully downloads the file 
    or logs an error if the download fails.
    """
    logging.debug("Examining %s", url)
    try:
        # Using HEAD request to get the headers without downloading the file
        response = requests.head(url, timeout=10)
        if response.status_code == 200:
            content_length = int(response.headers.get('Content-Length', 0))

            # Check if file already exists and sizes match
            if os.path.exists(local_path) and \
               os.path.getsize(local_path) == content_length:
                logging.debug(
                    "File already exists and sizes match, skipping download: %s",
                    local_path)
                return

            # Download the file if it doesn’t exist or sizes don’t match
            logging.info("Attempting to download %s to %s", url, local_path)
            response = requests.get(url, stream=True, timeout=10)
            response.raise_for_status()

            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            with open(local_path, 'wb') as file:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)

            logging.info("Successfully downloaded %s", url)

    except requests.exceptions.RequestException as e:
        logging.error("Failed to download %s to %s: %s",
                      url, local_path, str(e))

def process_link(link, local_path, url, executor):
    """
    Process a link extracted from a webpage.

    This function takes a link element from a webpage and processes it based on
    certain conditions. It checks if the link is valid and not of specific
    types (e.g., self-referencing, with prefixes, etc.). If the link is eligible
    for processing, it constructs the full URL, determines the local path for
    saving the downloaded content, and performs checks based on date strings and
    prefixes. Depending on the link's characteristics, it either downloads a
    file or prepares to create a directory for further exploration.

    Parameters:
    link (bs4.element.Tag): The link element to process.
    local_path (str): The local directory path where downloaded files or
                      directories will be stored.
    url (str): The base URL being processed.

    Returns:
    None: The function either processes the link and performs relevant actions
          or returns early if the link doesn't meet the specified conditions.
    """
    href = link.get('href')
    if not href or href.startswith("?prefix=") or \
        href.startswith("../") or href.startswith("./"):
        return

    full_url = urljoin(BASE_URL, href)
    next_local_path = os.path.join(local_path,
                                   urlparse(full_url).path.strip('/'))

    if full_url == url:
        logging.debug("Skipping self reference: %s", full_url)
        return

    if not any(prefix in full_url for prefix in ignore_date_check_prefixes):
        sibling = link.find_previous_sibling(string=True)
        if sibling:
            date_str = sibling.strip().split()[0]
            logging.debug("Preceding text to %s: %s", href, date_str)
            try:
                parsed_timestamp = dateutil_parser.parse(date_str)
                if parsed_timestamp.tzinfo is None:
                    parsed_timestamp = parsed_timestamp.replace(
                        tzinfo=timezone.utc
                    )
                if parsed_timestamp < cutoff_date:
                    logging.debug(
                        "File created before %s, skipping download: %s",
                        cutoff_date, date_str
                    )
                    return
            except ValueError as ve:
                logging.debug("Unable to parse date string %s: %s",
                              date_str, str(ve))

    if href.endswith('/') or href.startswith('?prefix='):
        logging.debug("Preparing to create directory: %s", next_local_path)
        os.makedirs(next_local_path, exist_ok=True)
        logging.debug("Identified as directory. Created directory %s",
                      next_local_path)
        process_url(full_url, next_local_path)
    else:
        executor.submit(download_file, full_url, next_local_path)


def process_url(url, local_path):
    """
    Processes a given URL by scraping its content and downloading relevant
    files.

    This function navigates to the specified URL, checks if it has been
    processed before, and if not, scrapes the webpage for links. It then
    downloads the content linked in these URLs into a local directory,
    considering various conditions such as URL prefixes, date checks, and 
    directory creation.

    The function uses concurrent futures to manage multiple download tasks 
    simultaneously, enhancing efficiency. It handles exceptions and logs 
    relevant information during the process.

    Parameters:
    url (str): The URL of the webpage to process.
    local_path (str): The local directory path where the downloaded files 
                      will be stored.

    Note:
    - The function relies on global variables such as `processed_urls`, 
      `driver`, `args`, `BASE_URL`, `ignore_date_check_prefixes`, and 
      `cutoff_date`.
    - It requires the `BeautifulSoup`, `requests`, and `concurrent.futures` 
      libraries, among others.

    Returns:
    None: The function does not return a value but downloads files and logs 
          information about the process.
    """
    logging.info("Processing URL: %s", url)
    if url in processed_urls:
        logging.debug("URL %s already processed, skipping...", url)
        return
    processed_urls.add(url)

    driver.get(url)
    try:
        wait.until(EC.presence_of_element_located(
                    (By.XPATH, '//*[@id="listing"]//a'))
                  )
    except TimeoutException as e:
        logging.error("Timeout while waiting for content to load for %s: %s",
                      url, str(e))
        logging.debug("Page Source:")
        print(driver.page_source)
        return
    except WebDriverException as e:
        logging.error("WebDriver encountered an error while loading content "
                      "for %s: %s", url, str(e))
        logging.debug("Page Source:")
        print(driver.page_source)
        return

    soup = BeautifulSoup(driver.page_source, 'html.parser')
    listing_div = soup.find(id='listing')

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.workers) as executor:
        for link in listing_div.find_all('a'):
            process_link(link, local_path, url, executor)


if __name__ == "__main__":
    try:
        process_url(BASE_URL, output_dir)
        logging.info("Finished downloading images.")
        driver.quit()
    except KeyboardInterrupt:
        logging.error("Interrupted by user. Exiting...")
        sys.exit(1)
