#!/usr/bin/env python3
# version 0.1
import os
import sys
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
import concurrent.futures
from datetime import datetime, timedelta, timezone
from dateutil import parser as dateutil_parser
import re
import logging
import argparse

# Set up argument parsing
parser = argparse.ArgumentParser(description='Manage binary images from Mirantis CDN.')
parser.add_argument('--workers', type=int, default=5, help='Number of worker threads to use')
parser.add_argument('--loglevel', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], help='Set the logging level')
parser.add_argument('--months', type=int, default=12, help='Number of months to consider for updating images')
parser.add_argument('--repo', help='Specify a specific repository or prefix to download', default=None)
args = parser.parse_args()

# Set up logging based on the command-line argument
numeric_level = getattr(logging, args.loglevel.upper(), None)
if not isinstance(numeric_level, int):
    raise ValueError('Invalid log level: %s' % args.loglevel)
logging.basicConfig(level=numeric_level, format='%(asctime)s - %(levelname)s - %(message)s')

# Cutoff date to not download all repos
cutoff_date = datetime.now(timezone.utc) - timedelta(days=args.months * 30)  # Approximate the number of days
logging.info(f"Cutoff date: {cutoff_date}")

# Determine if the --months argument was specified by the user
months_specified = args.months != 12  # Assuming 12 is the default value

base_url = "https://binary.mirantis.com/"
if args.repo:
    base_url = f"{base_url}?prefix={args.repo}"

output_dir = "/images/binaries"
processed_urls = set()

# List of prefixes to ignore the 2023 check
ignore_date_check_prefixes = ["stacklight/helm/", "bm/stub"]

# Configure the WebDriver
options = webdriver.ChromeOptions()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 10)

def download_url(url, local_path):
    logging.debug(f"Examining {url}")
    try:
        response = requests.head(url)  # Using HEAD request to get the headers without downloading the file
        if response.status_code == 200:
            content_length = int(response.headers.get('Content-Length', 0))
            
            # Check if file already exists and sizes match
            if os.path.exists(local_path) and os.path.getsize(local_path) == content_length:
                logging.debug(f"File already exists and sizes match, skipping download: {local_path}")
                return

            # Download the file if it doesn’t exist or sizes don’t match
            logging.info(f"Attempting to download {url} to {local_path}")
            response = requests.get(url, stream=True)
            response.raise_for_status()

            os.makedirs(os.path.dirname(local_path), exist_ok=True)

            with open(local_path, 'wb') as file:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)

            logging.info(f"Successfully downloaded {url}")

    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to download {url} to {local_path}: {str(e)}")

def process_url(url, local_path):
    logging.info(f"Processing URL: {url}")
    if url in processed_urls:
        logging.debug(f"URL {url} already processed, skipping...")
        return
    processed_urls.add(url)
    
    driver.get(url)
    try:
        wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="listing"]//a')))
    except Exception as e:
        logging.error(f"Failed to load content for {url}: {str(e)}")
        logging.debug("Page Source:")
        print(driver.page_source)
        return
    
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    listing_div = soup.find(id='listing')
    
    download_futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        for link in listing_div.find_all('a'):
            href = link.get('href')
            if href and not href.startswith("?prefix=") and not href.startswith("../") and not href.startswith("./"):
                full_url = urljoin(base_url, href)

                if full_url not in processed_urls:
                    next_local_path = os.path.join(local_path, urlparse(full_url).path.strip('/'))

                    logging.debug(f"Found link: {href}")
                    logging.debug(f"Next URL: {full_url}")
                    logging.debug(f"Next Local Path: {next_local_path}")

                    if full_url == url:
                        logging.debug(f"Skipping self reference: {full_url}")
                        continue

                    # Check if URL contains any of the specified prefixes
                    ignore_date_check = any(prefix in full_url for prefix in ignore_date_check_prefixes)

                    should_skip = False  # Flag to determine whether to skip processing the next URL

                    if not ignore_date_check:
                        # Extracting the creation date from the preceding text node
                        sibling = link.find_previous_sibling(string=True)

                        if sibling:
                            # Stripping leading and trailing whitespace characters
                            date_str = sibling.strip().split()[0]  # Take only the first part of the string
                            logging.debug(f"Preceding text to {href}: {date_str}")


                            # If the adjacent text looks like a date string, try to parse it
                            try:
                                parsed_timestamp = dateutil_parser.parse(date_str)
                                if parsed_timestamp.tzinfo is None:  # If the timestamp is offset-naive
                                    parsed_timestamp = parsed_timestamp.replace(tzinfo=timezone.utc)  # Assume UTC
                                if parsed_timestamp < cutoff_date:
                                    logging.debug(f"File created before {cutoff_date}, skipping download: {date_str}")
                                    should_skip = True
                            except ValueError as ve:
                                logging.debug(f"Unable to parse date string {date_str}: {str(ve)}")

                    if href.endswith('/') or href.startswith('?prefix='):
                        logging.debug(f"Preparing to create directory: {next_local_path}")
                        os.makedirs(next_local_path, exist_ok=True)
                        logging.debug(f"Identified as directory. Created directory {next_local_path}")
                        process_url(full_url, next_local_path)
                    elif not should_skip:  # Only download the file if should_skip flag is False
                        future = executor.submit(download_url, full_url, next_local_path)
                        download_futures.append(future)
        
        for future in concurrent.futures.as_completed(download_futures):
            try:
                future.result()
            except Exception as e:
                logging.error(f"Download task failed: {str(e)}")

if __name__ == "__main__":
    try:
        process_url(base_url, output_dir)
        logging.info("Finished downloading images.")
        driver.quit()
    except KeyboardInterrupt:
        logging.error("Interrupted by user. Exiting...")
        sys.exit(1)
