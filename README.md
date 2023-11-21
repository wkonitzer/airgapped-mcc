Copy mirror.sh onto image cache server, make executable and run

It will download the other scripts from this repo

It's safe to run this script multiple times if errors are encountered downloading images

This script was intended to be run on Equinix Metal s3.xlarge.86 servers on Ubuntu 20.04

"Usage: $0 {setup-mirror-server|setup-airgap-server|download-images|upload-images|sync-images|init} <release_version>"

<release_version> is only required for setup-mirror-server and init

setup-mirror-server: configure the mirror server
setup-airgap-server: install packages on airgap server
download-images: sync download of images from online to local cache
upload-images: upload docker images to local registry
sync-images: perform download-images and then upload-images
init: perform setup-mirror-server and then sync-images
