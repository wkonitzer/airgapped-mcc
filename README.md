Copy mirror.sh onto image cache server and run

It will download the other scripts from this repo

On client machine copy the /images/certs/myCA.crt file from the image cache
server to /usr/local/share/ca-certificates/ then run "update-ca-certificates"

Finally on client machine point dns at the image cache server
