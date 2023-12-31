# Mirror.sh Script Installation and Execution Guide

## Overview
This guide details the process for using the `mirror.sh` script on image cache servers. The script was tested on Equinix Metal s3.xlarge.86 servers running Ubuntu 20.04. 

For customer use, mount a 4TB USB drive to /images on a server running Ubuntu 20.04 and run `./mirror.sh init 17.0.0 usb` to create your mirror files to take onsite.

## Quickstart
- Login into a Ubuntu 20.04 Server connected to the internet
- Connect a 4TB USB drive and mount it to /images
- `wget https://raw.githubusercontent.com/wkonitzer/airgapped-mcc/main/mirror.sh && chmod +x mirror.sh`
- `sudo -i`
- `screen`
- `export AZURE_USER=<azure username>`
- `export AZURE_PASSWORD=<azure_password>`
- `./mirror.sh init 17.0.0 usb`

Wait some time for images to download. You can see logs in /tmp/
- apt_mirror.log
- download_py.log
- image_sync_py.log
- image_sync_upload_py.log
- installation.log

Once complete, power off the server and disconnect the USB drive.

In your airgapped environment:
- Select another Ubuntu 20.04 server as you airgapped server
- Connect the USB drive and mount it to /images
- run /images/mirrors.sh setup-airgap-server

Now point your MCC/MOSK install at the airgapped server. The proxy CA cert can be found at /images/certs/myCA.crt

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
- `init` and `setup-mirror-server` take an optional 3rd parameter `usb` to skip lvm creation. The intent is you would mount your usb drive (4TB) to `/images`

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

### 7. Logs
- Logs are written to `/tmp`
- Debug logging is available in the dependency scripts (`download.py` and `image-sync.py`) if you need to run them manually

### Notes
By default it syncs 12 months of images from Azure and the binary CDN delivery network, but you can add a <month> parameter to `download-images`, `upload-images` or `sync-images`  to change that.
e.g. `./mirror.sh download-images 6`
for 6 months worth of images.

