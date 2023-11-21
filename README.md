# Mirror.sh Script Installation and Execution Guide

## Overview
This guide details the process for using the `mirror.sh` script on image cache servers. The script is designed for use on Equinix Metal s3.xlarge.86 servers running Ubuntu 20.04.

## Steps for Use

### 1. Copying and Running the Script
- Transfer `mirror.sh` to your image cache server.
- Change the script's permissions to make it executable.
- Execute the script. It will automatically download additional scripts from this repository.

### 2. Handling Errors
- If you encounter errors during image downloads, it is safe to re-run the script multiple times.

### 3. Usage Instructions
- Command format: `$0 [command] <release_version>`
- Example: `./mirror.sh setup-mirror-server 17.0.0`
- Note: The `<release_version>` argument is only necessary for `setup-mirror-server` and `init` commands.

### 4. Commands
- `setup-mirror-server`: Configures the mirror server.
- `setup-airgap-server`: Installs necessary packages on the airgap server.
- `download-images`: Synchronizes the download of images from online to the local cache.
- `upload-images`: Uploads Docker images to the local registry.
- `sync-images`: Executes both `download-images` and `upload-images`.
- `init`: Runs `setup-mirror-server` followed by `sync-images`.

### 5. Storage Configuration
- Attach a 4TB USB drive to your mirror server at the `/images` mount point.
- After synchronization is complete, you may shut down the server and disconnect the drive.

### 6. Setting Up the Airgap Server
- Attach the same USB drive to the airgap server at the `/images` mount point.
- Run `/images/mirror.sh setup-airgap-server` for configuration.
- Once configured, use the airgap server as the proxy in MCC/MOSK installations.

### Notes
By default it syncs 12 months of images from Azure and the binary CDN delivery network, but you can add a <month> parameter to download-images to change that.
e.g. ./mirror.sh download-images 6
for 6 months worth of images.

