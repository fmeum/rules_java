import json
import os
import re
import requests
import hashlib
import time
import shutil # For downloading files

# Configuration
BCR_PATH = "../bazel-central-registry"  # Relative to rules_java checkout root, assumes script is run from rules_java
MODULES_PATH = os.path.join(BCR_PATH, "modules") # Path to BCR modules directory
DIRHASH_OUTPUT_PATH = ".github/bcr_dirhashes" # Relative to rules_java checkout root, where dirhash files will be stored
GITHUB_ARCHIVE_PATTERN = r"https://github.com/([^/]+)/([^/]+)/archive/refs/tags/(.*)" # Regex to parse GitHub archive URLs
# proxy.golang.org details
GOPROXY_URL = "https://proxy.golang.org" # Base URL for Go proxy
POLL_INTERVAL_SECONDS = 10 # How often to poll Go proxy for .info file
POLL_TIMEOUT_SECONDS = 300 # Max time to wait for Go proxy to cache a module (5 minutes)

# Ensure output directory for dirhashes exists
os.makedirs(DIRHASH_OUTPUT_PATH, exist_ok=True)

def get_module_and_version_from_url(url):
    """
    Extracts the Go module path and version from a GitHub archive URL.

    The function specifically targets URLs matching the GITHUB_ARCHIVE_PATTERN,
    which are typically of the form:
    https://github.com/<owner>/<repo>/archive/refs/tags/<tag>.<extension>

    Args:
        url (str): The GitHub archive URL.

    Returns:
        tuple: (module_path, version) or (None, None) if the URL doesn't match.
               module_path is formatted as "github.com/owner/repo".
               version is the extracted tag, with common extensions (.tar.gz, .zip) removed.
    """
    match = re.match(GITHUB_ARCHIVE_PATTERN, url)
    if not match:
        return None, None
    owner, repo, tag_and_extension = match.groups()

    # Attempt to remove common extensions like .tar.gz or .zip
    tag = tag_and_extension
    if tag.endswith(".tar.gz"):
        tag = tag[:-len(".tar.gz")]
    elif tag.endswith(".zip"):
        tag = tag[:-len(".zip")]

    # Construct the module path as expected by Go proxy (e.g., github.com/owner/repo)
    module_path = f"github.com/{owner}/{repo}"
    version = tag # The version is the tag itself
    return module_path, version

def fetch_from_goproxy(module_path, version):
    """
    Fetches the .info metadata file from proxy.golang.org for a given module and version.

    Args:
        module_path (str): The Go module path (e.g., "github.com/owner/repo").
        version (str): The module version (e.g., "v1.2.3").

    Returns:
        dict: The JSON response from the Go proxy as a dictionary if successful,
              None otherwise.
    """
    info_url = f"{GOPROXY_URL}/{module_path}/@v/{version}.info"
    print(f"Requesting: {info_url}")
    try:
        response = requests.get(info_url, timeout=30) # Use a reasonable timeout for the request
        response.raise_for_status() # Raise an HTTPError for bad status codes (4xx or 5xx)
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching .info from Go proxy for {module_path}@{version}: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from Go proxy for {module_path}@{version}: {e}. Response text: {response.text}")
        return None


def poll_for_goproxy_cache(module_path, version):
    """
    Polls the Go proxy for a module's .info file until it's cached or a timeout occurs.

    The function repeatedly calls `fetch_from_goproxy` until the 'Time' field is present
    in the response (indicating it's cached) and a 'Dirhash' is found.

    Args:
        module_path (str): The Go module path.
        version (str): The module version.

    Returns:
        str: The 'Dirhash' string if found within the timeout period, None otherwise.
    """
    start_time = time.time()
    while time.time() - start_time < POLL_TIMEOUT_SECONDS:
        info_data = fetch_from_goproxy(module_path, version)
        if info_data and "Time" in info_data: # 'Time' field indicates the module is cached by the proxy
            print(f"Module {module_path}@{version} is cached. Time: {info_data['Time']}")

            # Attempt to find 'Dirhash'. It can be in a few places.
            dirhash = info_data.get("Dirhash") # Common for actual Go modules
            if not dirhash and info_data.get("Origin"): # Sometimes nested under 'Origin'
                 dirhash = info_data["Origin"].get("Dirhash")

            if dirhash:
                return dirhash # Successfully found dirhash
            else:
                # If 'Dirhash' is not found, it means the proxy cached it but didn't provide this specific hash.
                # This can happen for non-Go modules or archives that don't fit the standard Go module structure.
                # The script is specifically looking for 'Dirhash' as per requirements.
                print(f"Warning: 'Dirhash' not found directly in .info for {module_path}@{version} even though it's cached. Full info: {info_data}")
                return None # Dirhash not found, even if cached.

        # Log that the module is not yet cached or the response is incomplete
        print(f"Module {module_path}@{version} not yet cached by Go proxy or 'Time' field missing. Waiting...")
        time.sleep(POLL_INTERVAL_SECONDS) # Wait before retrying

    print(f"Timeout waiting for {module_path}@{version} to be cached by Go proxy or for 'Dirhash' to appear.")
    return None

def download_file(url, destination_path):
    """
    Downloads a file from a URL to a specified destination.

    Args:
        url (str): The URL to download from.
        destination_path (str): The local path to save the downloaded file.

    Returns:
        bool: True if download was successful, False otherwise.
    """
    print(f"Downloading {url} to {destination_path}")
    try:
        # Use stream=True for efficient downloading of potentially large files
        with requests.get(url, stream=True, timeout=60) as r: # Timeout for the entire download
            r.raise_for_status() # Check for HTTP errors
            with open(destination_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192): # Download in chunks
                    f.write(chunk)
        return True
    except requests.exceptions.RequestException as e:
        print(f"Error downloading {url}: {e}")
        return False

def calculate_sha256(filepath):
    """
    Calculates the SHA256 hash of a file.

    Args:
        filepath (str): The path to the file.

    Returns:
        str: The hexadecimal SHA256 hash string of the file.
    """
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        # Read the file in chunks to handle large files efficiently
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def main():
    """
    Main function to drive the BCR module processing.

    - Walks through the MODULES_PATH in the Bazel Central Registry.
    - Processes 'source.json' files for each module.
    - Filters for GitHub archive URLs.
    - For each relevant URL:
        - Derives Go module path and version.
        - Downloads the archive to calculate its SHA256 hash (content_sha256).
        - Uses content_sha256 as the filename for storing the dirhash.
        - Checks if a dirhash file for this content_sha256 already exists.
        - If not, it interacts with the Go proxy to obtain the 'Dirhash'.
        - Stores the 'Dirhash' in '.github/bcr_dirhashes/<content_sha256>'.
    """
    print(f"Starting BCR module processing. BCR path: {BCR_PATH}, Output path: {DIRHASH_OUTPUT_PATH}")
    found_source_files = 0
    processed_urls = 0
    new_dirhashes_written = 0

    # Walk through the modules directory in the BCR
    for root, _, files in os.walk(MODULES_PATH):
        for filename in files:
            if filename == "source.json": # We are interested in source.json files
                found_source_files += 1
                filepath = os.path.join(root, filename)
                print(f"Processing {filepath}")
                try:
                    with open(filepath, 'r') as f:
                        data = json.load(f) # Load the source.json content

                    url = data.get("url") # Get the source archive URL
                    if not url:
                        print(f"No 'url' field in {filepath}")
                        continue

                    # Filter for URLs matching the GitHub archive pattern
                    if not re.match(GITHUB_ARCHIVE_PATTERN, url):
                        # print(f"Skipping URL (not a matching GitHub archive tag): {url}")
                        continue

                    print(f"Found matching GitHub archive URL: {url}")
                    processed_urls += 1

                    module_path, version = get_module_and_version_from_url(url)
                    if not module_path or not version:
                        print(f"Could not derive module/version from URL: {url}")
                        continue

                    print(f"Derived Go module: {module_path}, version: {version}")

                    # Download the original archive to calculate its content SHA256.
                    # This SHA256 will be used as the filename for the dirhash file,
                    # ensuring that if the archive content changes (even if URL/version doesn't),
                    # we treat it as a new entry.
                    temp_archive_path = "temp_archive_download.tmp" # Temporary path for downloaded archive
                    if not download_file(url, temp_archive_path):
                        print(f"Failed to download original archive from {url}. Skipping.")
                        # Ensure temp file is removed if download failed partway
                        if os.path.exists(temp_archive_path): os.remove(temp_archive_path)
                        continue

                    content_sha256 = calculate_sha256(temp_archive_path) # Calculate SHA256 of the downloaded content
                    dirhash_filename = os.path.join(DIRHASH_OUTPUT_PATH, content_sha256) # Name dirhash file after content SHA256

                    # Check if a dirhash file for this content SHA256 already exists
                    if os.path.exists(dirhash_filename):
                        print(f"Dirhash file {dirhash_filename} already exists for content SHA256 {content_sha256} (URL: {url}). Skipping proxy operations.")
                        if os.path.exists(temp_archive_path): os.remove(temp_archive_path) # Clean up downloaded archive
                        continue

                    # If dirhash file doesn't exist, proceed to interact with Go proxy
                    print(f"Requesting {module_path}@{version} to be cached by Go proxy (for URL {url})...")
                    # Initial request to trigger caching. Some proxies might need an explicit first hit.
                    # The response of this first call isn't critical, polling will verify.
                    fetch_from_goproxy(module_path, version)

                    # Poll Go proxy until the dirhash is available or timeout
                    dirhash = poll_for_goproxy_cache(module_path, version)

                    if dirhash:
                        print(f"Successfully fetched dirhash for {module_path}@{version} (URL: {url}): {dirhash}")
                        # Write the obtained dirhash to the file named after the content_sha256
                        with open(dirhash_filename, 'w') as f_out:
                            f_out.write(dirhash)
                        new_dirhashes_written +=1
                        print(f"Written dirhash to {dirhash_filename}")
                    else:
                        print(f"Could not obtain dirhash for {module_path}@{version} (URL: {url}) from Go proxy after polling.")

                    # Clean up the temporary downloaded archive
                    if os.path.exists(temp_archive_path):
                        os.remove(temp_archive_path)

                except json.JSONDecodeError:
                    print(f"Error decoding JSON from {filepath}")
                except Exception as e: # Catch any other unexpected errors during processing of a single source.json
                    print(f"An unexpected error occurred processing {filepath}: {e}")
                    # Ensure cleanup if an error occurred after download but before explicit removal
                    if 'temp_archive_path' in locals() and os.path.exists(temp_archive_path):
                         os.remove(temp_archive_path)

    print(f"Script finished.")
    print(f"Found {found_source_files} source.json files.")
    print(f"Processed {processed_urls} matching GitHub archive URLs.")
    print(f"Written {new_dirhashes_written} new dirhash files to {DIRHASH_OUTPUT_PATH}.")

if __name__ == "__main__":
    main()
