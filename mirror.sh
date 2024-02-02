#!/bin/bash
# This script sets up a mirror of images required to install
# Mirantis MOSK

# Automatically exit on error
set -e

# Global configuration
# Storage location
IMAGES_DIR="/images"

LOG_FILE="/tmp/installation.log"

# Global flag
airgapserver=false # Set this to true if operating on an air-gapped server

# Log Function
log() {
    echo "$(date +%Y-%m-%dT%H:%M:%S) - $1" | tee -a "$LOG_FILE"
}

# Check if the script is running as root or via sudo
if [[ $EUID -ne 0 ]]; then
   log "This script must be run as root. Please use sudo or log in as root."
   exit 1
fi

# Get the first argument to determine the operation
operation="$1"

# Function to perform initial checks
perform_checks() {
    # Check if running inside a screen session
    if [ -z "$STY" ]; then
        log "This script is not running inside a screen session."
        log "Please run this script inside a screen session."
        exit 1
    fi

    # Check for AZURE_USER and AZURE_PASSWORD environment variables
    if [[ -z "${AZURE_USER}" ]]; then
        log "Environment variable AZURE_USER is not set. Please set it and try again."
        exit 1
    fi

    if [[ -z "${AZURE_PASSWORD}" ]]; then
        log "Environment variable AZURE_PASSWORD is not set. Please set it and try again."
        exit 1
    fi
}

# See https://mirantis.jira.com/wiki/spaces/NG/pages/1891926183/CDN+Mirrors+and+structure+of+repositories
# for Azure readonly credentials

# Define the usage message
USAGE_MESSAGE="Usage: $0 {setup-mirror-server|setup-airgap-server|download-images|upload-images|sync-images|init} <release_version>\ne.g. ./mirror.sh init 17.0.0"

# Case statement for handling different operations
case "$1" in
    setup-mirror-server | init | setup-airgap-server | download-images | upload-images | sync-images)
        # Perform checks only if the operation is not 'setup-airgap-server'
        if [ "$1" != "setup-airgap-server" ]; then
            perform_checks
        fi
        ;;
    *)
        echo -e "$USAGE_MESSAGE"
        exit 1
        ;;
esac

# Check if Logical Volume already exists
lv_exists() {
    lvdisplay /dev/images/images > /dev/null 2>&1
}

# Function to check if a drive is unassigned
is_drive_unassigned() {
    local drive=$1
    local mount_point
    mount_point=$(lsblk -no MOUNTPOINT "/dev/$drive")

    # If the mount point is empty, the drive is unassigned
    [[ -z "$mount_point" ]]
}

# Function to create Logical Volume
create_lv() {
    log "Starting Logical Volume creation"
    if lv_exists; then
        log "Logical Volume 'images' already exists. Skipping creation."
        return
    fi

    # Find all unassigned drives
    unassigned_drives=()
    for drive in $(lsblk -nd --output NAME | grep -v "^loop"); do
        if is_drive_unassigned "$drive"; then
            unassigned_drives+=("/dev/$drive")
        fi
    done

    # Exit if no unassigned drives found
    if [ ${#unassigned_drives[@]} -eq 0 ]; then
        log "No unassigned drives found."
        return
    fi

    log "Unassigned drives found: ${unassigned_drives[*]}"

    # Creating Physical Volumes
    for drive in "${unassigned_drives[@]}"; do
        log "Creating Physical Volume on $drive"
        pvcreate "$drive"
    done

    # Creating a single Volume Group named 'images'
    log "Creating Volume Group 'images'"
    vgcreate images "${unassigned_drives[@]}"

    # Creating Logical Volume
    log "Creating Logical Volume 'images' in Volume Group 'images'"
    lvcreate -l 100%FREE -n images images

    # Formatting and Mounting the Logical Volume
    log "Formatting the Logical Volume with ext4"
    mkfs.ext4 /dev/images/images

    # Making mount directory and mounting the volume
    log "Mounting the Logical Volume"
    mkdir -p "$IMAGES_DIR"
    mount /dev/images/images "$IMAGES_DIR/"

    # Adding entry to fstab, checking if not already present
    if ! grep -qs '/dev/images/images $IMAGES_DIR ' /etc/fstab ; then
        log "/dev/images/images $IMAGES_DIR ext4 defaults 0 2" >> /etc/fstab
        log "fstab entry added for $IMAGES_DIR"
    else
        log "fstab entry for $IMAGES_DIR already exists. Skipping addition."
    fi

    log "Logical Volume 'images' created and mounted at $IMAGES_DIR"
}

# Function to setup dnsmasq
setup_dnsmasq() {
    # Fetch the primary IP address
    primary_ip=$(ip route get 1 | awk -F 'src ' '{print $2}' | awk '{print $1}')

    log "Primary IP Address: $primary_ip"

    if [ -z "$primary_ip" ]; then
        log "Could not determine the primary IP address. Trying alternative method"
        primary_ip=$(ip -4 addr show scope global | awk '/inet/ {print $2}' | cut -d/ -f1 | head -n1)

        if [ -z "$primary_ip" ]; then
            log "Could not determine the primary IP address. Please check network configuration."
            return 1
        fi
    fi

    # Check if the airgapserver flag is set
    if [ "$airgapserver" = true ]; then
        log "Airgap server mode is enabled. Checking if dnsmasq is installed..."

        # Check if dnsmasq is installed
        if ! command -v dnsmasq >/dev/null 2>&1; then
            log "Error: dnsmasq is not installed. Please install it manually."
            exit 1
        else
            log "dnsmasq is already installed."
        fi
    else
        # If not in airgap server mode, install dnsmasq if it's not already installed
        if ! command -v dnsmasq >/dev/null 2>&1; then
            log "Installing dnsmasq..."
            apt-get update
            apt-get install -y dnsmasq
        else
            log "dnsmasq is already installed."
        fi
    fi

    # Setting up dnsmasq configuration
    log "Configuring dnsmasq..."
    cat > /etc/dnsmasq.d/local-mirror.conf <<EOF
address=/binary.mirantis.com/$primary_ip
address=/mirror.mirantis.com/$primary_ip
address=/mirantis.azurecr.io/$primary_ip
address=/repos.mirantis.com/$primary_ip
address=/deb.nodesource.com/$primary_ip
EOF

    # Update /etc/dnsmasq.conf if not already configured
    if ! grep -q "listen-address=$primary_ip" /etc/dnsmasq.conf; then
        log "Updating /etc/dnsmasq.conf..."
        echo "listen-address=$primary_ip" >> /etc/dnsmasq.conf
    fi
    if ! grep -q "^bind-interfaces" /etc/dnsmasq.conf; then
        echo "bind-interfaces" >> /etc/dnsmasq.conf
    fi

    # Restart dnsmasq to apply changes
    log "Restarting dnsmasq..."
    systemctl restart dnsmasq
    log "dnsmasq has been configured and restarted."
}

setup_aptmirror() {
    # Install apt-mirror
    log "Installing apt-mirror..."
    if ! apt-get install -y apt-mirror; then
        log "Failed to install apt-mirror. Exiting."
        exit 1
    fi

    # Downloading the specified version of apt-mirror
    log "Updating apt-mirror..."
    apt_mirror_url="https://raw.githubusercontent.com/wkonitzer/apt-mirror/master/apt-mirror"
    if wget -O /tmp/apt-mirror "$apt_mirror_url"; then
      # Replace the existing apt-mirror binary
      if mv /tmp/apt-mirror /usr/bin/apt-mirror; then
          chmod +x /usr/bin/apt-mirror
          log "Successfully updated apt-mirror."
      else
          log "Failed to update /usr/bin/apt-mirror. Exiting."
          exit 1
      fi
    else
        log "Failed to download the updated apt-mirror script. Exiting."
        exit 1
    fi
}

setup_aptmirror_config() {
    local cluster_release=$1
    local yaml_url="https://binary.mirantis.com/releases/cluster/${cluster_release}.yaml"
    local yaml_file="/tmp/${cluster_release}.yaml"
    local repo_value
    local mirror_list_file="/etc/apt/mirror.list"
    local version_number

    # Download the YAML file
    log "Downloading YAML file for cluster release ${cluster_release}..."
    if ! wget -O "$yaml_file" "$yaml_url"; then
        log "Failed to download YAML file. Exiting."
        exit 1
    fi

    # Parse YAML file to extract kaas_ubuntu_repo
    log "Extracting kaas_ubuntu_repo from YAML file..."
    repo_value=$(grep 'kaas_ubuntu_repo:' "$yaml_file" | head -n 1 | awk '{print $2}')
    if [ -z "$repo_value" ]; then
        log "kaas_ubuntu_repo could not be extracted. Exiting."
        exit 1
    fi

    # Remove the "kaas/" prefix if present
    repo_value=${repo_value#kaas/}

    # Extract version number for the Mirantis repository
    version_number=$(grep 'mcr:' "$yaml_file" | awk '{print $2}' | cut -d '.' -f1-2)  

    # Create apt-mirror config
    log "Creating apt-mirror configuration..."
    cat > "$mirror_list_file" <<- EOM
############# config ##################
#
set base_path    $IMAGES_DIR/apt-mirror
#
set var_path     \$base_path/var
set postmirror_script \$var_path/postmirror.sh
set run_postmirror 1
set nthreads     20
set _tilde 0
set use_acquire_by_hash no
#
############# end config ##############

deb [arch=amd64] https://mirror.mirantis.com/kaas/$repo_value focal main restricted universe
deb [arch=amd64] https://mirror.mirantis.com/kaas/$repo_value focal-updates main restricted universe
deb [arch=amd64] https://mirror.mirantis.com/kaas/$repo_value focal-security main restricted universe
clean https://mirror.mirantis.com/kaas/$repo_value

# Additional repositories
deb https://mirror.mirantis.com/kaas/kubernetes-extra-0.0.9/focal focal main
clean https://mirror.mirantis.com/kaas/kubernetes-extra-0.0.9/focal

deb https://deb.nodesource.com/node_20.x nodistro main
clean https://deb.nodesource.com/node_20.x

deb [arch=amd64] https://repos.mirantis.com/ubuntu focal stable-$version_number
clean https://repos.mirantis.com/ubuntu
EOM

    log "apt-mirror configuration created at $mirror_list_file"

    # Create the directory for postmirror.sh if it doesn't exist
    log "Creating directory for postmirror.sh..."
    local postmirror_dir="$IMAGES_DIR/apt-mirror/var"
    mkdir -p "$postmirror_dir"

    # Create postmirror.sh with the specified content and make it executable
    local postmirror_script="${postmirror_dir}/postmirror.sh"
    log "Creating and setting execute permission for postmirror.sh..."
    cat > "$postmirror_script" <<- EOM
#!/bin/bash

# Create the directory for the first key if it does not exist and download the key
mkdir -p $IMAGES_DIR/apt-mirror/mirror/mirror.mirantis.com/kaas/kubernetes-extra-0.0.9/focal
wget -q -O $IMAGES_DIR/apt-mirror/mirror/mirror.mirantis.com/kaas/kubernetes-extra-0.0.9/focal/archive-kubernetes-extra-0.0.9.key https://mirror.mirantis.com/kaas/kubernetes-extra-0.0.9/focal/archive-kubernetes-extra-0.0.9.key

# Download the second key
wget -q -O /images/apt-mirror/mirror/repos.mirantis.com/ubuntu/gpg https://repos.mirantis.com/ubuntu/gpg

# Create the directory for the third key if it does not exist and download the key
mkdir -p $IMAGES_DIR/apt-mirror/mirror/deb.nodesource.com/gpgkey
wget -q -O $IMAGES_DIR/apt-mirror/mirror/deb.nodesource.com/gpgkey/nodesource-repo.gpg.key https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key
EOM
    chmod +x "$postmirror_script"

    log "postmirror.sh script created and made executable at $postmirror_script"
}

create_certificates() {
    local certs_dir="$IMAGES_DIR/certs"
    local config_file="/etc/dnsmasq.d/local-mirror.conf"
    local domains

    # Create certs directory
    mkdir -p "$certs_dir"

    # Save current directory
    local original_dir=$(pwd)    
    cd "$certs_dir"

    # Generate CA key and certificate only if they don't already exist
    if [[ ! -f myCA.key ]] || [[ ! -f myCA.pem ]]; then
        openssl genrsa -out myCA.key 2048
        openssl req -x509 -new -nodes -key myCA.key -sha256 -days 1024 -out myCA.pem -subj "/CN=Mirantis Custom CA"
        openssl x509 -in myCA.pem -inform PEM -out myCA.crt
    else
        log "CA key and certificate already exist, using existing files."
    fi

    # Extract domains from config file
    domains=$(grep 'address=/' "$config_file" | cut -d'/' -f2)

    for domain in $domains; do
        # Generate private key and CSR for each domain
        openssl genrsa -out "$domain.key" 2048
        openssl req -new -key "$domain.key" -out "$domain.csr" -subj "/CN=$domain"

        # Create extension file
        cat > "$domain.ext" <<- EOM
authorityKeyIdentifier=keyid,issuer
basicConstraints=CA:FALSE
keyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment
subjectAltName = @alt_names

[alt_names]
DNS.1 = $domain
EOM

        # Sign the CSR with the CA certificate
        openssl x509 -req -in "$domain.csr" -CA myCA.pem -CAkey myCA.key -CAcreateserial -out "$domain.crt" -days 825 -sha256 -extfile "$domain.ext"
    done

    # Change back to the original directory
    cd "$original_dir"
}

# Update the ca certificates
update_ca_certificates() {
    log "Update CA certificates"
    # Copy the certificate to the desired directory
    cp $IMAGES_DIR/certs/myCA.crt /usr/local/share/ca-certificates/

    # Update CA certificates
    update-ca-certificates
}

# Function to setup Nginx
setup_nginx() {
    log "Setup Nginx"
    mkdir -p /etc/nginx/conf.d/
    mkdir -p /var/log/nginx

    # Configure nginx for each domain in /etc/dnsmasq.d/local-mirror.conf, except for mirantis.azurecr.io
    echo "" > /etc/nginx/conf.d/default.conf
    while IFS= read -r line; do
        domain=$(log "$line" | awk -F '/' '{print $2}')
    
        # Skip mirantis.azurecr.io and binary.mirantis.com
        if [ "$domain" != "mirantis.azurecr.io" ] && [ "$domain" != "binary.mirantis.com" ]; then
            cat << EOF >> /etc/nginx/conf.d/default.conf
server {
    listen 443 ssl;
    server_name $domain;

    ssl_certificate $IMAGES_DIR/certs/$domain.crt;
    ssl_certificate_key $IMAGES_DIR/certs/$domain.key;

    # Set root directory
    root $IMAGES_DIR/apt-mirror/mirror/$domain/;

    location / {
        autoindex on;
    }

    # Additional SSL settings
    ssl_session_cache builtin:1000 shared:SSL:10m;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-CHACHA20-POLY1305';
    ssl_prefer_server_ciphers on;
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name $domain;
    return 301 https://\$host\$request_uri;
}
EOF
        fi
    done < /etc/dnsmasq.d/local-mirror.conf

    # Specific configuration for binary.mirantis.com
    domain="binary.mirantis.com"
    cat << EOF >> /etc/nginx/conf.d/default.conf
server {
    listen 443 ssl;
    server_name $domain;

    ssl_certificate $IMAGES_DIR/certs/$domain.crt;
    ssl_certificate_key $IMAGES_DIR/certs/$domain.key;

    # Custom root directory for binary.mirantis.com
    root /images/binaries/;

    location / {
        autoindex on;
    }

    # Additional SSL settings
    ssl_session_cache builtin:1000 shared:SSL:10m;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-CHACHA20-POLY1305';
    ssl_prefer_server_ciphers on;
}

# Redirect HTTP to HTTPS
server {
    listen 80;
    server_name $domain;
    return 301 https://\$host\$request_uri;
}
EOF

    # Add additional server block for Docker registry
    cat << EOF >> /etc/nginx/conf.d/default.conf
server {
    listen 443 ssl;
    server_name mirantis.azurecr.io;

    # Increase the client body size limit to handle large Docker image layers
    client_max_body_size 0;

    # SSL configuration
    ssl_certificate $IMAGES_DIR/certs/mirantis.azurecr.io.crt;
    ssl_certificate_key $IMAGES_DIR/certs/mirantis.azurecr.io.key;

    # Proxy to your Docker registry
    location / {
        proxy_pass http://registry:5000;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # Ensure large buffers for Docker image layers
        proxy_buffers 64 128k;
        proxy_buffer_size 64k;
        proxy_busy_buffers_size 256k;
    }
}
EOF

    local container_name="my-nginx"
    local running_container
    local stopped_container

    log "Creating nginx container"

    # Check if the container is already running
    running_container=$(docker ps -q -f name=^/my-nginx$)
    stopped_container=$(docker ps -aq -f status=exited -f name=^/my-nginx$)
    if [ -n "$running_container" ]; then
        log "Container $container_name is already running. Stopping and removing it..."
        docker stop "$container_name"
        docker rm "$container_name"
    elif [ -n "$stopped_container" ]; then
        log "Container $container_name exists but is stopped. Removing it..."
        docker rm "$container_name"
    fi

    log "Starting new $container_name container..."

    # Start the new container
    docker run --name "$container_name" -p 80:80 -p 443:443 \
        -v /etc/nginx/conf.d:/etc/nginx/conf.d \
        -v /var/log/nginx:/var/log/nginx \
        -v /images:/images \
        --restart always -d nginx

    create_docker_network
    connect_to_network "my-nginx"
    connect_to_network "registry"
}

# Function to create a Docker network if it doesn't already exist
create_docker_network() {
    local network_name="my-network"

    # Check if the network already exists
    if docker network ls --format "{{.Name}}" | grep -wq "^${network_name}$"; then
        echo "Network '${network_name}' already exists."
    else
        echo "Creating Docker network: ${network_name}"
        docker network create "${network_name}"
    fi
}

# Connect containers to network
connect_to_network() {
    local network_name="my-network"
    local container_name="$1"

    # Check if the container is already connected to the network
    if docker network inspect "$network_name" --format '{{json .Containers}}' | grep -q "$container_name"; then
        echo "Container '$container_name' is already connected to the network '$network_name'."
    else
        echo "Connecting '$container_name' to the network '$network_name'."
        docker network connect "$network_name" "$container_name"
    fi
}

download_mcr() {
    log "Download and install MCR"
    local cluster_release=$1
    local yaml_url="https://binary.mirantis.com/releases/cluster/${cluster_release}.yaml"
    local yaml_file="/tmp/${cluster_release}.yaml"
    local version_number
    local version
    local containerd
    TEMP_DIR=$(mktemp -d)

    # Download the YAML file
    log "Downloading YAML file for cluster release ${cluster_release}..."
    if ! wget -O "$yaml_file" "$yaml_url"; then
        log "Failed to download YAML file. Exiting."
        exit 1
    fi

    version_number=$(grep 'mcr:' "$yaml_file" | awk '{print $2}' | cut -d '.' -f1-2)
    version=$(grep 'mcr:' "$yaml_file" | awk '{print $2}') 
    containerd=$(grep 'containerd_version_map:' "$yaml_file" | awk -F'[ ,}]' '{for(i=1;i<=NF;i++) if($i=="Debian:") {print $(i+1); exit}}' | tr -d '\n')

    local download_dir="$IMAGES_DIR/downloaded_packages/"
    mkdir -p "$download_dir"

    # Save current directory
    local original_dir=$(pwd)

    # Change to download directory
    cd "$TEMP_DIR" || exit

    wget "https://repos.mirantis.com/ubuntu/dists/focal/pool/stable-$version_number/amd64/docker-ee_$version~3-0~ubuntu-focal_amd64.deb" \
         "https://repos.mirantis.com/ubuntu/dists/focal/pool/stable-$version_number/amd64/docker-ee-cli_$version~3-0~ubuntu-focal_amd64.deb" \
         "https://repos.mirantis.com/ubuntu/dists/focal/pool/stable-$version_number/amd64/containerd.io_$containerd-1_amd64.deb" || {
        log "Failed to download packages. Exiting."
        cd "$original_dir"
        exit 1
    }

    dpkg -i *.deb || {
        log "Failed to install packages. Exiting."
        cd "$original_dir"
        exit 1
    }

    mv *.deb "$download_dir/"

    # Change back to the original directory
    cd "$original_dir"
}

# Function to setup a local registry
setup_docker_registry() {
    log "Setup docker"

    # Configure Docker to use a custom data directory
    mkdir -p /etc/docker
    mkdir -p $IMAGES_DIR/docker/data/directory

    # Creating /etc/docker/daemon.json with the custom data directory
    printf '{\n  "data-root": "%s"\n}\n' "$IMAGES_DIR/docker/data/directory" > /etc/docker/daemon.json

    # Restart Docker to apply new configuration
    log "Restarting docker..this may take some time."
    systemctl restart docker

    # Check if the Docker registry container is already running
    running_container=$(docker ps -q -f name=^/registry$)
    if [ -n "$running_container" ]; then
        log "Docker registry container is already running."
    else
        log "Running a Docker registry container..."
        docker run -d \
          -p 5001:5000 \
          --restart=always \
          --name registry \
          registry:2
    fi

    log "Docker and local registry setup complete."
}

setup_python_extras() {
    log "Installing Python packages"
    # install pip3
    apt install python3-pip -y

    # Install selenium
    pip3 install selenium

    # Download and install Google Chrome
    wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    dpkg -i google-chrome-stable_current_amd64.deb || apt-get install -f -y

    # Get the version of Chrome
    chrome_version=$(google-chrome --version | awk '{print $3}')
    
    # Extract the major version
    chrome_major_version=$(echo "$chrome_version" | cut -d '.' -f 1)

    # Download the corresponding ChromeDriver
    wget https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_$chrome_major_version
    chromedriver_version=$(cat LATEST_RELEASE_$chrome_major_version)
    wget https://edgedl.me.gvt1.com/edgedl/chrome/chrome-for-testing/$chromedriver_version/linux64/chromedriver-linux64.zip

    # Unzip and move ChromeDriver to /usr/bin/
    apt install unzip -y
    unzip -o chromedriver-linux64.zip
    mv chromedriver-linux64/chromedriver /usr/bin/

    # Clean up
    rm google-chrome-stable_current_amd64.deb chromedriver-linux64.zip LATEST_RELEASE_$chrome_major_version

    # Install other Python packages
    pip3 install beautifulsoup4
    pip3 install docker
    pip3 install azure-mgmt-containerregistry
    pip3 install azure-identity
    pip3 install install python-dateutil
}

setup_azure_cli() {
    # Check if azure-cli is already installed
    if az --version &> /dev/null; then
        log "Azure CLI is already installed."
    else
        log "Installing Azure CLI..."
        curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
    fi

    # Login to Azure
    az login -u "$AZURE_USER" -p "$AZURE_PASSWORD"

    # Check if the login was successful
    if az account show; then
        log "Azure login successful."
    else
        log "Azure login failed."
        return 1
    fi
}

# Setup tinyproxy
install_and_configure_tinyproxy() {
    # Check if the airgapserver flag is set
    if [ "$airgapserver" = true ]; then
        log "Airgap server mode is enabled. Checking if tinyproxy is installed..."

        # Check if tinyproxy is installed
        if ! command -v tinyproxy >/dev/null 2>&1; then
            log "Error: tinyproxy is not installed. Please install it manually."
            exit 1
        else
            log "tinyproxy is already installed."
        fi
    else
        # If not in airgap server mode, install tinyproxy if it's not already installed
        if ! command -v tinyproxy >/dev/null 2>&1; then
            log "Installing tinyproxy..."
            apt-get install -y tinyproxy
        else
            log "tinyproxy is already installed."
        fi
    fi

    # Create systemd timer for tinyproxy if it doesn't exist
    local timer_file="/etc/systemd/system/tinyproxy-restart.timer"
    if [ ! -f "$timer_file" ]; then
        log "Creating tinyproxy restart timer..."
        cat > "$timer_file" <<- EOF
[Unit]
Description=Restart tinyproxy every 3 hours

[Timer]
OnBootSec=3h
OnUnitActiveSec=3h
Unit=tinyproxy.service

[Install]
WantedBy=timers.target
EOF
        systemctl daemon-reload
        systemctl enable tinyproxy-restart.timer
        systemctl start tinyproxy-restart.timer
    else
        log "tinyproxy restart timer already exists."
    fi

    # Update tinyproxy configuration
    local config_file="/etc/tinyproxy/tinyproxy.conf"
    if ! grep -q "^Allow 0.0.0.0/0" "$config_file"; then
        log "Updating tinyproxy configuration..."
        echo "Allow 0.0.0.0/0" >> "$config_file"
        systemctl restart tinyproxy
    else
        log "tinyproxy configuration already updated."
    fi
}

# Build updated Tiny proxy
build_tinyproxy() {
    log "Building tinyproxy"

    # Install required packages
    apt update
    apt install -y git build-essential automake

    # Clone the repository if it doesn't already exist
    if [ ! -d "tinyproxy" ]; then
        git clone https://github.com/tinyproxy/tinyproxy.git || { log "Failed to clone repository"; return 1; }
    fi

    # Build and install Tinyproxy
    pushd tinyproxy
    ./autogen.sh && ./configure && make
    if [ $? -eq 0 ]; then
        make install
        cp /usr/local/bin/tinyproxy /images/other_packages/
        popd
    else
        log "Build failed"
        popd
        return 1
    fi
}

fix_tinyproxy() {
    log "Updating tinyproxy"

    systemctl stop tinyproxy-restart.timer
    systemctl stop tinyproxy

    # Check if the new binary exists
    if [ -f "/images/other_packages/tinyproxy" ]; then

        # Replace the binary
        cp /images/other_packages/tinyproxy /usr/bin/tinyproxy
    else
        log "New Tinyproxy binary not found."
        return 1
    fi

    # Replace the systemd service file
    bash -c 'cat > /lib/systemd/system/tinyproxy.service' << EOF
[Unit]
Description=Tinyproxy lightweight HTTP Proxy
After=network.target
Documentation=man:tinyproxy(8) man:tinyproxy.conf(5)

[Service]
Type=forking
ExecStart=/usr/bin/tinyproxy -c /etc/tinyproxy/tinyproxy.conf
PIDFile=/run/tinyproxy/tinyproxy.pid

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl start tinyproxy-restart.timer
    systemctl start tinyproxy
}
   
# Function to check log file for errors
check_log_for_errors() {
    local log_file=$1
    # Exclude specific line, then check for errors
    if grep -v "Post Mirror script has completed. See above output for any possible errors." "$log_file" | grep -Ei "error|failed"; then
        log "Error found in log file: $log_file"
        return 1
    fi
}

download_all_images() {
    # Define the directory where the files will be downloaded
    download_dir="$IMAGES_DIR"

    local months="$1"

    # File URLs to download
    declare -A files=(
        ["download.py"]="https://raw.githubusercontent.com/wkonitzer/airgapped-mcc/main/download.py"
        ["image_sync.py"]="https://raw.githubusercontent.com/wkonitzer/airgapped-mcc/main/image_sync.py"
    )

    # Change to the download directory
    pushd "$IMAGES_DIR" > /dev/null

    # Download the files
    for file in "${!files[@]}"; do
        wget -N "${files[$file]}"
        chmod +x "$file"
    done

    # Change back to the original directory
    popd > /dev/null

    # Run download.py, pull_images.sh, and apt-mirror in parallel
    log "Starting download.py, image_sync.py, and apt-mirror in parallel..."
    
    if [ -n "$months" ]; then
        python3 "$download_dir/download.py" --months "$months" &>/tmp/download_py.log &
    else
        python3 "$download_dir/download.py" &>/tmp/download_py.log &
    fi
    download_pid=$!  # Capture PID of download.py
    
    if [ -n "$months" ]; then
        python3 "$download_dir/image_sync.py" --months "$months" download &>/tmp/image_sync_py.log &
    else
        python3 "$download_dir/image_sync.py" download &>/tmp/image_sync_py.log &
    fi
    image_sync_pid=$!  # Capture PID of image_sync.py
    
    /usr/bin/apt-mirror &>/tmp/apt_mirror.log &
    apt_mirror_pid=$!  # Capture PID of apt-mirror

    # Spinner for all processes
    log "Waiting for all images to download..."
    spinner="/|\\-/|\\-"
    while kill -0 $download_pid 2>/dev/null || kill -0 $image_sync_pid 2>/dev/null || kill -0 $apt_mirror_pid 2>/dev/null; do
        for i in $(seq 0 7); do
            printf "\r${spinner:$i:1}"
            sleep .1
        done
    done
    printf "\r"
    log "All images downloaded"

    # Check the log files for errors after all processes are done
    log "Checking log files for errors..."

    # Check /tmp/download_py.log
    check_log_for_errors "/tmp/download_py.log"
    if [ $? -eq 0 ]; then
        log "No errors found in /tmp/download_py.log."
    else
        log "Error found in /tmp/download_py.log, exiting"
        exit 1
    fi

    # Check /tmp/image_sync_py.log
    check_log_for_errors "/tmp/image_sync_py.log"
    if [ $? -eq 0 ]; then
        log "No errors found in /tmp/image_sync_py.log."
    else
        log "Error found in /tmp/image_sync_py.log, exiting"
        exit 1
    fi

    # Check /tmp/apt_mirror.log
    check_log_for_errors "/tmp/apt_mirror.log"
    if [ $? -eq 0 ]; then
        log "No errors found in /tmp/apt_mirror.log."
    else
        log "Error found in /tmp/apt_mirror.log, exiting"
        exit 1
    fi

    log "No errors found in image download log files."    
}

upload_all_images() {
    # Define the directory where script will be downloaded
    download_dir="$IMAGES_DIR"

    local months="$1"

    # URL and filename
    file_url="https://raw.githubusercontent.com/wkonitzer/airgapped-mcc/main/image_sync.py"
    file_name="image_sync.py"

    # Change to the download directory
    pushd "$IMAGES_DIR" > /dev/null 

    # Download the file only if it has been updated on the server
    wget -N "$file_url"

    # set execute permission
    chmod +x "$file_name"

    # Change back to the original directory
    popd > /dev/null

    # Once pull_images.sh is completed, run image_sync.py
    log "Starting image_sync upload"

    if [ -n "$months" ]; then
        python3 "$download_dir/image_sync.py" --months "$months" upload &>/tmp/image_sync_upload_py.log &
    else
        python3 "$download_dir/image_sync.py" upload &>/tmp/image_sync_upload_py.log &
    fi
    image_sync_pid=$!  # Capture PID of image_sync.py

    # Spinner for push_images.sh
    log "Waiting for image_sync to complete..."
    spinner="/|\\-/|\\-"
    while kill -0 $image_sync_pid 2>/dev/null; do
        for i in `seq 0 7`; do
            printf "\r${spinner:$i:1}"
            sleep .1
        done
    done
    printf "\r"
    
    log "All images pushed"

    # Check the log files for errors after all processes are done
    log "Checking log files for errors..."

    # Check /tmp/apt_mirror.log
    check_log_for_errors "/tmp/image_sync_upload_py.log"
    if [ $? -eq 0 ]; then
        log "No errors found in /tmp/image_sync_upload_py.log."
    else
        log "Error found in /tmp/image_sync_upload_py.log, exiting"
        exit 1
    fi    

    log "No errors found in image push log files."      
}

setup_etc_hosts() {
    local config_file="/etc/dnsmasq.d/local-mirror.conf"

    log "Reading domain and IP information from $config_file..."

    # Check if the configuration file exists
    if [ ! -f "$config_file" ]; then
        log "Configuration file $config_file not found. Please check the file path."
        return 1
    fi

    log "Configuring /etc/hosts..."

    # Add a marker before script's entries
    echo "# BEGIN Custom Script Entries" >> /etc/hosts    

    while IFS= read -r line; do
        # Extract the IP address and domain from each line
        if [[ "$line" =~ address=/(.+)/(.+) ]]; then
            local domain="${BASH_REMATCH[1]}"
            local ip="${BASH_REMATCH[2]}"

            # Check and update /etc/hosts
            if grep -qE "^$ip\s+$domain" /etc/hosts; then
                log "$domain already exists with the correct IP in /etc/hosts, skipping..."
            elif grep -qE "\s+$domain" /etc/hosts; then
                log "Updating $domain in /etc/hosts..."
                sed -i "/\s$domain/c\\$ip $domain" /etc/hosts
            else
                log "Adding $domain to /etc/hosts..."
                echo "$ip $domain" >> /etc/hosts
            fi
        fi
    done < "$config_file"

    # Add a marker after script's entries
    echo "# END Custom Script Entries" >> /etc/hosts    

    log "/etc/hosts updated"
}

remove_custom_hosts_entries() {
    # Check if the markers are present
    if grep -q "# BEGIN Custom Script Entries" /etc/hosts && grep -q "# END Custom Script Entries" /etc/hosts; then
        log "Removing custom entries from /etc/hosts..."
        sed -i '/# BEGIN Custom Script Entries/,/# END Custom Script Entries/d' /etc/hosts
        log "Custom entries removed."
    else
        log "No custom entries found in /etc/hosts."
    fi
}

disable_ipv6() {
    local config_file="/etc/sysctl.conf"
    local ipv6_disabled="net.ipv6.conf.all.disable_ipv6 = 1"
    local ipv6_default_disabled="net.ipv6.conf.default.disable_ipv6 = 1"
    local ipv6_loopback_disabled="net.ipv6.conf.lo.disable_ipv6 = 1"

    echo "Disabling IPv6..."

    # Function to append a line if it's not present in the file
    append_if_not_exists() {
        local line="$1"
        local file="$2"
        if ! grep -qF -- "$line" "$file"; then
            echo "$line" | sudo tee -a "$file" > /dev/null
            echo "Appended '$line' to $file"
        else
            echo "'$line' is already present in $file"
        fi
    }

    # Append IPv6 disable settings to /etc/sysctl.conf if not present
    append_if_not_exists "$ipv6_disabled" "$config_file"
    append_if_not_exists "$ipv6_default_disabled" "$config_file"
    append_if_not_exists "$ipv6_loopback_disabled" "$config_file"

    # Reload sysctl settings
    sudo sysctl -p

    echo "IPv6 has been disabled."
}

check_dependencies() {
    local dependencies=("wget" "openssl" "apt-rdepends")
    local missing_deps=()

    for dep in "${dependencies[@]}"; do
        if ! command -v "$dep" > /dev/null; then
            missing_deps+=("$dep")
        fi
    done

    if [ ${#missing_deps[@]} -ne 0 ]; then
        log "The following dependencies are missing: ${missing_deps[*]}"
        log "Attempting to install missing dependencies..."

        apt-get update || error_exit "Failed to update package lists."

        for dep in "${missing_deps[@]}"; do
            apt-get install -y "$dep" || error_exit "Failed to install $dep."
        done

        log "All missing dependencies installed successfully."
    fi
}

# Error Function
error_exit() {
    log "ERROR: $1"
    exit 1
}

# Global variable to keep track of downloaded packages
declare -A downloaded_pkgs

# Global variable to keep track of processed packages
declare -A processed_pkgs

# Function to download a single package
download_single_pkg() {
    local single_pkg=${1//<}  # Remove leading angle bracket
    single_pkg=${single_pkg//>}  # Remove trailing angle bracket

    # Check if the package has already been processed to avoid circular dependencies
    if [[ ${processed_pkgs[$single_pkg]} ]]; then
        return
    fi

    # Check if it's a real package or a virtual package
    log "Processing: $single_pkg"
    if apt-cache showpkg "$single_pkg" | grep -A1 'Versions:' | tail -n 1 | grep -E '([0-9]+(\.[0-9]+)+)' > /dev/null; then
        # It's a real package, mark as processed and download it
        processed_pkgs[$single_pkg]=1        
        sudo -u _apt apt-get download "$single_pkg" || {
            log "Warning: Failed to download $single_pkg"
            return
        }
    else
        # Handle virtual package by finding a real package that provides it
        local real_pkg=$(apt-cache --names-only search "^$single_pkg" | awk '{print $1}' | head -n 1)
        if [ -n "$real_pkg" ]; then
            log "Downloading $real_pkg as a replacement for virtual package $single_pkg"
            processed_pkgs[$single_pkg]=1
            sudo -u _apt apt-get download "$real_pkg"
            single_pkg="$real_pkg"  # Update single_pkg to the real package name
        else
            log "Info: $single_pkg is a virtual package or does not exist."
            return
        fi
    fi

    # Get dependencies of the package
    local dependencies=$(apt-cache depends "$single_pkg" | awk '/Depends:/ {print $2}')
    for dep in $dependencies; do
        download_single_pkg "$dep"
    done
}

# Function to download a package and its dependencies
download_package() {
    PACKAGE_NAME=$1
    DOWNLOAD_LOCATION=$2
    TEMP_DIR=$(mktemp -d)
    log "Downloading $PACKAGE_NAME and its dependencies to $TEMP_DIR..."

    # Change ownership of the temporary directory to '_apt' user
    chown _apt:root "$TEMP_DIR"    

    # Change to temporary directory
    pushd "$TEMP_DIR" > /dev/null 

    # Download the main package and resolve dependencies
    download_single_pkg "$PACKAGE_NAME"  

    # Move all downloaded packages to the desired location
    log "Moving downloaded packages to $DOWNLOAD_LOCATION..."
    mv *.deb "$DOWNLOAD_LOCATION/" 2>&1 | tee -a /tmp/move_log.txt   

    # Change back to the original directory
    popd > /dev/null
}

download_packages() {

    DOWNLOAD_DIR="$IMAGES_DIR/downloaded_packages"
    pushd "$DOWNLOAD_DIR" > /dev/null

    # Download each URL passed to the function
    for url in "$@"
    do
        log "Downloading $url"
        wget --no-clobber --timestamping "$url"
    done

    popd > /dev/null
}

create_client_install() {
    log "Creating airgapped-server install files"

    # Directory to store the downloaded packages
    DOWNLOAD_DIR="$IMAGES_DIR/downloaded_packages"
    mkdir -p "$DOWNLOAD_DIR"

    # Download Tinyproxy and its dependencies
    log "Downloading tinyproxy"
    download_package "tinyproxy" "$DOWNLOAD_DIR"

    # Download MCR ##
    log "Downloading MCR"

    # Download dnsmasq and its dependencies
    log "Downloading dnsmasq"
    download_package "dnsmasq" "$DOWNLOAD_DIR"

    # Download additional packages
    download_packages \
    "http://us.archive.ubuntu.com/ubuntu/pool/main/i/iptables/iptables_1.8.4-3ubuntu2.1_amd64.deb" \
    "http://us.archive.ubuntu.com/ubuntu/pool/main/i/iptables/libip6tc2_1.8.4-3ubuntu2.1_amd64.deb" \
    "http://us.archive.ubuntu.com/ubuntu/pool/main/s/systemd/libnss-systemd_245.4-4ubuntu3.22_amd64.deb" \
    "http://us.archive.ubuntu.com/ubuntu/pool/main/s/systemd/libpam-systemd_245.4-4ubuntu3.22_amd64.deb" \
    "http://us.archive.ubuntu.com/ubuntu/pool/main/s/systemd/udev_245.4-4ubuntu3.22_amd64.deb"

    log "All packages downloaded to $DOWNLOAD_DIR" 

    # Copy the running script to $IMAGES_DIR
    SCRIPT_PATH=$(readlink -f "$0")
    cp "$SCRIPT_PATH" "$IMAGES_DIR/"
    log "Script copied to $IMAGES_DIR"
}

# Function to install packages from a directory
install_downloaded_packages() {
    local error_occurred=0

    # Directory where the downloaded packages are stored
    DOWNLOAD_DIR="$IMAGES_DIR/downloaded_packages"

    # Check if the download directory exists
    if [ ! -d "$DOWNLOAD_DIR" ]; then
        log "Download directory does not exist: $DOWNLOAD_DIR"
        return 1
    fi

    # Change to the directory containing the downloaded packages
    pushd "$DOWNLOAD_DIR" > /dev/null

    # Check if there are any .deb files to install
    if [ -z "$(ls *.deb 2> /dev/null)" ]; then
        log "No .deb files found in $DOWNLOAD_DIR"
        popd > /dev/null
        return 1
    fi

    # Do pre-install
    log "Performing pre-install"
    dpkg -i *

    # Loop through all .deb files and install them
    for pkg in *.deb; do
        # Extract package name from .deb file
        PKG_NAME=$(dpkg-deb -f "$pkg" Package)

        # Check if package is already installed
        log "Installing package $PKG_NAME..."

        dpkg -i "$pkg"
        local status=$?

        # Check for error
        if [ $status -ne 0 ]; then
            log "Error installing package $PKG_NAME. Attempting to resolve..."

            # Specific check for 'chrony' and 'time-daemon' conflict
            if dpkg -i "$pkg" 2>&1 | grep -q "chrony conflicts with time-daemon"; then
                log "Unable to install 'chrony' due to conflict with 'time-daemon'. Removing the 'chrony' package file."
                rm -f "$pkg"
                continue  # Skip to the next package
            fi

            # Check for specific 'conflicting packages' error
            if dpkg -i "$pkg" 2>&1 | grep -q "conflicting packages"; then
                # Extract the name of the conflicting package
                local conflict_pkg=$(dpkg -i "$pkg" 2>&1 | sed -n 's/.*conflicts with \(.*\)/\1/p')
                    
                # Remove the conflicting package
                apt-get remove -y "$conflict_pkg"
                    
                # Attempt to reinstall the package
                dpkg -i "$pkg"
                status=$?
            fi

            if [ $status -ne 0 ]; then
                log "Error installing package $PKG_NAME after conflict resolution"
                error_occurred=1
                break  # Exit the loop on error
            fi
        fi
    done

    # Fix any broken dependencies
    apt-get -f install -y
    local status=$?

    if [ $status -ne 0 ]; then
        log "Error fixing broken dependencies"
        error_occurred=1
    fi

    # Change back to the original directory
    popd > /dev/null

    # Return the error flag
    return $error_occurred
}

# Function to check if specific services are running
check_services() {
    log "Checking if services are running"
    for service in nginx tinyproxy docker; do
        log "Checking $service"
        if ! systemctl is-active --quiet $service; then
            log "Service $service is not running."
            return 1
        fi
    done
    log "All required services are running."
}

install_packages() {
    local max_retries=10
    local attempts=0

    while [ $attempts -lt $max_retries ]; do
        install_downloaded_packages
        if [ $? -eq 0 ]; then
            log "All packages installed successfully."
            break
        else
            log "Retrying installation due to errors..."
            attempts=$((attempts + 1))
        fi
    done

    if [ $attempts -eq $max_retries ]; then
        log "Failed to install all packages after $max_retries attempts."
        
        # Call the check_services function
        check_services || exit 1
    fi
}

setup_airgap_server() {
    airgapserver=true
    export DEBIAN_FRONTEND=noninteractive
    disable_ipv6
    set +e

    install_packages

    set -e

    remove_custom_hosts_entries
    setup_dnsmasq
    update_ca_certificates
    setup_docker_registry
    setup_nginx    
    install_and_configure_tinyproxy
    setup_etc_hosts
    fix_tinyproxy
    log "Airgap server setup complete."
}    

setup_mirror_server() {
    local skip_create_lv=$1

    # Remove custom hosts entries
    remove_custom_hosts_entries

    # Disable ipv6
    disable_ipv6

    # Check dependencies
    check_dependencies

    # Setup storage, skip if flag is set
    if [ "$skip_create_lv" != "usb" ]; then
        create_lv
    fi

    # Setup DNS server
    setup_dnsmasq

    # Setup apt-mirror
    setup_aptmirror
    setup_aptmirror_config "$version"

    # Create certificates
    create_certificates
    update_ca_certificates

    # Setup docker registry
    download_mcr "$version"
    setup_docker_registry

    # Setup nginx
    setup_nginx

    # Setup python
    setup_python_extras

    # Setup Azure CLI
    setup_azure_cli

    # Setup Tinyproxy
    install_and_configure_tinyproxy
    build_tinyproxy
    fix_tinyproxy

    # Copy packages for airgap server
    create_client_install

    log "Mirror server setup complete."
}

download_images() {
    local months=$1

    remove_custom_hosts_entries
    setup_azure_cli

    # Download images
    download_all_images "$months"
}

upload_images() {
    local months=$1

    remove_custom_hosts_entries

    # swap endpoints /etc/hosts
    setup_etc_hosts

    # Upload images
    upload_all_images "$months"
}

sync_images() {
    local months=$1

    download_images "$months"
    upload_images "$months"
    log "Mirror creation complete"
}

log_error() {
    log "Error: $1"
    log "Usage: $0 $2"
    exit 1
}

check_version_format() {
    if ! [[ $1 =~ ^[0-9]+(\.[0-9]+)*$ ]]; then
        log_error "Invalid version format. Version should only contain numbers and dots." "$2"
    fi
}

check_month_format() {
    if [ -n "$1" ] && ! [[ "$1" =~ ^[0-9]+$ ]]; then
        log_error "Invalid month format. It should only contain numbers." "$2"
    fi
}

# Case statement for handling different operations
case "$1" in
    setup-mirror-server | init)
        [ -z "$2" ] && log_error "No release version provided." "$0 $1 <release_version> \nExample: $0 $1 17.0.0"
        version="$2"
        check_version_format "$version" "Example: $0 $1 17.0.0"
        skip_create_lv_flag="$3"
        [ -n "$3" ] && [ "$3" != "usb" ] && log_error "When provided, the third argument must be 'usb'."

        [ "$1" = "setup-mirror-server" ] && setup_mirror_server "$3"
        [ "$1" = "init" ] && { setup_mirror_server "$3"; sync_images; }
        ;;
    setup-airgap-server)
        setup_airgap_server
        ;;    
    download-images | upload-images | sync-images)
        months="$2"
        check_month_format "$months" "$0 $1 <months> \nExample: $0 $1 6"
        [ "$1" = "download-images" ] && download_images "$months"
        [ "$1" = "upload-images" ] && upload_images "$months"
        [ "$1" = "sync-images" ] && sync_images "$months"
        ;;
    *)
        echo -e "$USAGE_MESSAGE"
        exit 1
        ;;
esac
