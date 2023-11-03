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
import datetime
import re
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

base_url = "https://binary.mirantis.com/"
output_dir = "/images/binaries"
processed_urls = set()

# Configure the WebDriver
options = webdriver.ChromeOptions()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
driver = webdriver.Chrome(options=options)
wait = WebDriverWait(driver, 10)

def download_url(url, local_path):
    logger.debug(f"Examining {url}")
    try:
        response = requests.head(url)  # Using HEAD request to get the headers without downloading the file
        if response.status_code == 200:
            content_length = int(response.headers.get('Content-Length', 0))
            
            # Check if file already exists and sizes match
            if os.path.exists(local_path) and os.path.getsize(local_path) == content_length:
                logger.debug(f"File already exists and sizes match, skipping download: {local_path}")
                return
            
            # Download the file if it doesn’t exist or sizes don’t match
            logger.info(f"Attempting to download {url} to {local_path}")
            response = requests.get(url, stream=True)
            response.raise_for_status()
            
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            with open(local_path, 'wb') as file:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)
                
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to download {url} to {local_path}: {str(e)}")

def process_url(url, local_path):
    logger.info(f"Processing URL: {url}")
    if url in processed_urls:
        logger.debug(f"URL {url} already processed, skipping...")
        return
    processed_urls.add(url)
    
    driver.get(url)
    try:
        wait.until(EC.presence_of_element_located((By.XPATH, '//*[@id="listing"]//a')))
    except Exception as e:
        logger.error(f"Failed to load content for {url}: {str(e)}")
        logger.debug("Page Source:")
        print(driver.page_source)
        return
    
    soup = BeautifulSoup(driver.page_source, 'html.parser')
    listing_div = soup.find(id='listing')
    
    download_futures = []
    with concurrent.futures.ThreadPoolExecutor() as executor:
        for link in listing_div.find_all('a'):
            href = link.get('href')
            if href and href != "?prefix=":
                next_url = urljoin(base_url, href)
                next_local_path = os.path.join(local_path, urlparse(next_url).path.strip('/'))
                
                logger.debug(f"Found link: {href}")
                logger.debug(f"Next URL: {next_url}")
                logger.debug(f"Next Local Path: {next_local_path}")
                
                if next_url == url:
                    logger.debug(f"Skipping self reference: {next_url}")
                    continue
                
                should_skip = False  # Flag to determine whether to skip processing the next URL
                
                 # Extracting the creation date from the preceding text node
                sibling = link.find_previous_sibling(text=True)

                if sibling:
                    # Stripping leading and trailing whitespace characters
                    date_str = sibling.strip()
                    date_str = sibling.strip().split()[0]  # Take only the first part of the string
                    logger.debug(f"Preceding text to {href}: {date_str}")


                    # If the adjacent text looks like a date string, try to parse it
                    if re.match(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}.\d{3}Z', date_str):
                        try:
                            date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%dT%H:%M:%S.%fZ')
                            if date_obj.year < 2023:
                                logger.debug(f"File created before 2023, skipping download: {date_str}")
                                should_skip = True
                        except ValueError as ve:
                            logger.debug(f"Unable to parse date string {date_str}: {str(ve)}")
                
                if href.endswith('/') or href.startswith('?prefix='):
                    logger.debug(f"Preparing to create directory: {next_local_path}")
                    os.makedirs(next_local_path, exist_ok=True)
                    logger.debug(f"Identified as directory. Created directory {next_local_path}")
                    process_url(next_url, next_local_path)
                elif not should_skip:  # Only download the file if should_skip flag is False
                    future = executor.submit(download_url, next_url, next_local_path)
                    download_futures.append(future)
        
        for future in concurrent.futures.as_completed(download_futures):
            try:
                future.result()
            except Exception as e:
                logger.error(f"Download task failed: {str(e)}")

if __name__ == "__main__":
    try:
        process_url(base_url, output_dir)
        driver.quit()
    except KeyboardInterrupt:
        logger.error("Interrupted by user. Exiting...")
        sys.exit(1)
