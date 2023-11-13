#!/bin/bash

# Automatically exit on error
set -e

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
  if lv_exists; then
    echo "Logical Volume 'images' already exists. Skipping creation."
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
    echo "No unassigned drives found."
    return
  fi

  echo "Unassigned drives found: ${unassigned_drives[*]}"

  # Creating Physical Volumes
  for drive in "${unassigned_drives[@]}"; do
    echo "Creating Physical Volume on $drive"
    pvcreate "$drive"
  done

  # Creating a single Volume Group named 'images'
  echo "Creating Volume Group 'images'"
  vgcreate images "${unassigned_drives[@]}"

  # Creating Logical Volume
  echo "Creating Logical Volume 'images' in Volume Group 'images'"
  lvcreate -l 100%FREE -n images images

  # Formatting and Mounting the Logical Volume
  echo "Formatting the Logical Volume with ext4"
  mkfs.ext4 /dev/images/images

  # Making mount directory and mounting the volume
  echo "Mounting the Logical Volume"
  mkdir -p /images
  mount /dev/images/images /images/

  # Adding entry to fstab, checking if not already present
  if ! grep -qs '/dev/images/images /images ' /etc/fstab ; then
    echo "/dev/images/images /images ext4 defaults 0 2" >> /etc/fstab
    echo "fstab entry added for /images"
  else
    echo "fstab entry for /images already exists. Skipping addition."
  fi

  echo "Logical Volume 'images' created and mounted at /images"
}

# Function to setup dnsmasq
setup_dnsmasq() {
  # Fetch the primary IP address
  primary_ip=$(ip route get 1 | awk -F 'src ' '{print $2}' | awk '{print $1}')

  echo "Primary IP Address: $primary_ip"

  if [ -z "$primary_ip" ]; then
    echo "Could not determine the primary IP address. Please check network configuration."
    return 1
  fi

  # Check if dnsmasq is installed, install if not
  if ! command -v dnsmasq >/dev/null 2>&1; then
    echo "Installing dnsmasq..."
    apt-get update
    apt-get install -y dnsmasq
  else
    echo "dnsmasq is already installed."
  fi

  # Setting up dnsmasq configuration
  echo "Configuring dnsmasq..."
  cat > /etc/dnsmasq.d/local-mirror.conf <<EOF
address=/binary.mirantis.com/$primary_ip
address=/mirror.mirantis.com/$primary_ip
address=/mirantis.azurecr.io/$primary_ip
address=/repos.mirantis.com/$primary_ip
address=/deb.nodesource.com/$primary_ip
EOF

  # Update /etc/dnsmasq.conf
  echo "Updating /etc/dnsmasq.conf..."
  echo "listen-address=$primary_ip" >> /etc/dnsmasq.conf
  echo "bind-interfaces" >> /etc/dnsmasq.conf

  # Restart dnsmasq to apply changes
  echo "Restarting dnsmasq..."
  systemctl restart dnsmasq
  echo "dnsmasq has been configured and restarted."
}

setup_aptmirror() {
  # Install apt-mirror
  echo "Installing apt-mirror..."
  if ! apt-get update || ! apt-get install -y apt-mirror; then
    echo "Failed to install apt-mirror. Exiting."
    exit 1
  fi

  # Downloading the specified version of apt-mirror
  echo "Updating apt-mirror..."
  apt_mirror_url="https://raw.githubusercontent.com/wkonitzer/apt-mirror/master/apt-mirror"
  if wget -O /tmp/apt-mirror "$apt_mirror_url"; then
    # Replace the existing apt-mirror binary
    if mv /tmp/apt-mirror /usr/bin/apt-mirror; then
      chmod +x /usr/bin/apt-mirror
      echo "Successfully updated apt-mirror."
    else
      echo "Failed to update /usr/bin/apt-mirror. Exiting."
      exit 1
    fi
  else
    echo "Failed to download the updated apt-mirror script. Exiting."
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
  echo "Downloading YAML file for cluster release ${cluster_release}..."
  if ! wget -O "$yaml_file" "$yaml_url"; then
    echo "Failed to download YAML file. Exiting."
    exit 1
  fi

  # Parse YAML file to extract kaas_ubuntu_repo
  echo "Extracting kaas_ubuntu_repo from YAML file..."
  repo_value=$(grep 'kaas_ubuntu_repo:' "$yaml_file" | head -n 1 | awk '{print $2}')
  if [ -z "$repo_value" ]; then
    echo "kaas_ubuntu_repo could not be extracted. Exiting."
    exit 1
  fi

  # Remove the "kaas/" prefix if present
  repo_value=${repo_value#kaas/}

  # Extract version number for the Mirantis repository
  version_number=$(grep 'mcr:' "$yaml_file" | awk '{print $2}' | cut -d '.' -f1-2)  

  # Create apt-mirror config
  echo "Creating apt-mirror configuration..."
  cat > "$mirror_list_file" <<- EOM
############# config ##################
#
set base_path    /images/apt-mirror
#
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

  echo "apt-mirror configuration created at $mirror_list_file"
}

create_certificates() {
    local certs_dir="/images/certs"
    local config_file="/etc/dnsmasq.d/local-mirror.conf"
    local domains

    # Create certs directory
    mkdir -p "$certs_dir"
    cd "$certs_dir"

    # Generate CA key and certificate only if they don't already exist
    if [[ ! -f myCA.key ]] || [[ ! -f myCA.pem ]]; then
        openssl genrsa -out myCA.key 2048
        openssl req -x509 -new -nodes -key myCA.key -sha256 -days 1024 -out myCA.pem -subj "/CN=Mirantis Custom CA"
        openssl x509 -in myCA.pem -inform PEM -out myCA.crt
    else
        echo "CA key and certificate already exist, using existing files."
    fi

    # Extract domains from config file
    domains=$(grep 'address=/' "$config_file" | cut -d'/' -f3)

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
}

# Function to setup Nginx
setup_nginx() {
  # Install nginx
  apt-get update
  apt-get install -y nginx

  # Configure nginx for each domain in /etc/dnsmasq.d/local-mirror.conf, except for mirantis.azurecr.io
  echo "" > /etc/nginx/conf.d/mirrors.conf
  while IFS= read -r line; do
    domain=$(echo "$line" | awk -F '/' '{print $2}')
    
    # Skip mirantis.azurecr.io
    if [ "$domain" != "mirantis.azurecr.io" ]; then
      cat << EOF >> /etc/nginx/conf.d/mirrors.conf
server {
    listen 443 ssl;
    server_name $domain;

    ssl_certificate /images/certs/$domain.crt;
    ssl_certificate_key /images/certs/$domain.key;

    # Set root directory
    root /images/apt-mirror/mirror/$domain/;

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

  # Add additional server block for Docker registry
  cat << EOF >> /etc/nginx/conf.d/mirrors.conf
server {
    listen 443 ssl;
    server_name mirantis.azurecr.io;

    # Increase the client body size limit to handle large Docker image layers
    client_max_body_size 1000M;

    # SSL configuration
    ssl_certificate /images/certs/mirantis.azurecr.io.crt;
    ssl_certificate_key /images/certs/mirantis.azurecr.io.key;

    # Proxy to your Docker registry
    location / {
        proxy_pass http://localhost:5001;
        proxy_set_header Host \$http_host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # Ensure large buffers for Docker image layers
        proxy_buffers 32 16k;
        proxy_buffer_size 32k;

        # Other necessary proxy settings...
    }

    # Other settings...
}
EOF

  # Test nginx configuration
  if nginx -t; then
    # Restart Nginx to apply new configuration
    systemctl restart nginx
    echo "Nginx configuration is valid and the service has been restarted."
  else
    echo "Error in Nginx configuration. Please check the configuration file."
    return 1
  fi
}

# Function to install Docker and setup a local registry
setup_docker_registry() {
  # Install Docker and jq
  apt-get update
  apt-get install -y docker.io jq

  # Configure Docker to use a custom data directory
  mkdir -p /etc/docker
  mkdir -p /images/docker/data/directory

  # Creating /etc/docker/daemon.json with the custom data directory
  jq -n --arg dataRoot "/images/docker/data/directory" '{"data-root": $dataRoot}' > /etc/docker/daemon.json

  # Restart Docker to apply new configuration
  systemctl restart docker

  # Run a Docker registry container
  docker run -d \
    -p 5001:5000 \
    --restart=always \
    --name registry \
    registry:2

  echo "Docker and local registry setup complete."
}

setup_python_extras() {
    # Install selenium
    pip3 install selenium

    # Download and install Google Chrome
    wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    dpkg -i google-chrome-stable_current_amd64.deb || apt-get install -f

    # Get the version of Chrome
    chrome_version=$(google-chrome --version | awk '{print $3}')
    
    # Extract the major version
    chrome_major_version=$(echo "$chrome_version" | cut -d '.' -f 1)

    # Download the corresponding ChromeDriver
    wget https://chromedriver.storage.googleapis.com/LATEST_RELEASE_$chrome_major_version
    chromedriver_version=$(cat LATEST_RELEASE_$chrome_major_version)
    wget https://chromedriver.storage.googleapis.com/$chromedriver_version/chromedriver_linux64.zip

    # Unzip and move ChromeDriver to /usr/bin/
    unzip chromedriver_linux64.zip
    mv chromedriver /usr/bin/

    # Clean up
    rm google-chrome-stable_current_amd64.deb chromedriver_linux64.zip LATEST_RELEASE_$chrome_major_version

    # Install other Python packages
    pip3 install beautifulsoup4
}

setup_azure_cli() {
    # Install azure-cli
    curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash

    # Set up environment variables for Azure CLI login
    # Replace these with your actual Service Principal details or other authentication methods
    export AZURE_USER="<your_username>"
    export AZURE_PASSWORD="<your_password>"

    # Login to Azure
    az login -u "$AZURE_USER" -p "$AZURE_PASSWORD"

    # Check if the login was successful
    if az account show; then
        echo "Azure login successful."
    else
        echo "Azure login failed."
        return 1
    fi
}

download_additional_keys() {
    # Key and directory pairs
    declare -A keys=(
        ["/images/apt-mirror/mirror/mirror.mirantis.com/kubernetes-extra-0.0.9/focal"]="https://mirror.mirantis.com/kaas/kubernetes-extra-0.0.9/focal/archive-kubernetes-extra-0.0.9.key"
        ["/images/apt-mirror/mirror/repos.mirantis.com/ubuntu"]="https://repos.mirantis.com/ubuntu/gpg"
        ["/images/apt-mirror/mirror/deb.nodesource.com/gpgkey"]="https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key"
    )

    # Iterate over the keys and directories
    for dir in "${!keys[@]}"; do
        local key_url="${keys[$dir]}"
        local filename=$(basename "$key_url")

        # Create the directory if it doesn't exist
        mkdir -p "$dir"

        # Change to the directory
        cd "$dir"

        # Download the key
        echo "Downloading key from $key_url to $dir"
        wget "$key_url" -O "$filename"

        # Check if the download was successful
        if [[ -f "$filename" ]]; then
            echo "Key $filename downloaded successfully to $dir."
        else
            echo "Failed to download the key $filename to $dir."
            return 1
        fi
    done
}

download_and_execute_scripts() {
    # Define the directory where the files will be downloaded
    download_dir="/images"
    mkdir -p "$download_dir"

    # File URLs to download
    declare -A files=(
        ["download.py"]="https://raw.githubusercontent.com/wkonitzer/airgapped-mcc/main/download.py"
        ["pull_images.sh"]="https://raw.githubusercontent.com/wkonitzer/airgapped-mcc/main/pull_images.sh"
        ["push_images.sh"]="https://raw.githubusercontent.com/wkonitzer/airgapped-mcc/main/push_images.sh"
    )

    # Download the files
    for file in "${!files[@]}"; do
        wget "${files[$file]}" -O "$download_dir/$file" && chmod +x "$download_dir/$file"
    done

    # Run download.py and pull_images.sh in parallel
    echo "Starting download.py and pull_images.sh in parallel..."
    python3 "$download_dir/download.py" &>/tmp/download_py.log &
    bash "$download_dir/pull_images.sh" &>/tmp/pull_images_sh.log &

    # Run apt-mirror
    echo "Starting apt-mirror..."
    /usr/bin/apt-mirror &>/tmp/apt_mirror.log &

    # Spinner for pull_images.sh
    echo "Waiting for pull_images.sh to complete..."
    spinner="/|\\-/|\\-"
    while kill -0 $! 2>/dev/null; do
        for i in `seq 0 7`; do
            printf "\r${spinner:$i:1}"
            sleep .1
        done
    done
    printf "\r"

    # Once pull_images.sh is completed, run push_images.sh
    echo "Starting push_images.sh..."
    bash "$download_dir/push_images.sh" &>/tmp/push_images_sh.log &

    # Spinner for push_images.sh
    echo "Waiting for push_images.sh to complete..."
    while kill -0 $! 2>/dev/null; do
        for i in `seq 0 7`; do
            printf "\r${spinner:$i:1}"
            sleep .1
        done
    done
    printf "\r"
    
    echo "Script execution completed."
}


# Calling the create_lv function
create_lv

# Setup DNS server
setup_dnsmasq

# Setup apt-mirror
setup_aptmirror
setup_aptmirror_config "15.0.4"

# Create certificates
create_certificates

# Setup nginx
setup_nginx

# Setup docker registry
setup_docker_registry

# Setup python
setup_python_extras

# Setup Azure CLI
setup_azure_cli

# Download additional keys
download_additional_keys

echo "Setup complete.. starting mirror creation"

download_and_execute_scripts
