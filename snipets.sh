
du -d1 -h --apparent-size /home/mandeep/test
du -d1 -h --apparent-size --exclude=/{proc,sys,dev,run} /*

sudo docker system prune -af --volumes
sudo journalctl --rotate 
sudo journalctl --vacuum-time=1s
sudo find /var/log/container -type f -mtime +90
sudo find /var/log/container -type f -mtime +30 -delete
sudo find /var/log/atop -type f -mtime +0
sudo find /var/log/ -type f -regex '.*log\.[1-9].*'
sudo find /var/log/ -type f -regex '.*log\.[1-9].*' -delete

sudo lsof -nP +L1
find /proc/*/fd -ls | grep  '(deleted)'
sudo lsof -nP +L1 | awk '{sum+=$7;} END {print sum/1024/1024" MB";}'

To truncate it:

: > /path/to/the/file.log
If it was already deleted, on Linux, you can still truncate it by doing:

: > "/proc/$pid/fd/$fd"
Where $pid is the process id of the process that has the file opened, and $fd one file descriptor it has it opened under (which you can check with lsof -p "$pid".

sudo journalctl -n 50 -f -u docker-<cont_name>

l=0; for c in $(sudo docker ps --format '{{.ID}}'); do l=$[ $l + $(sudo docker inspect -f '{{.HostConfig.Memory}}' $c) ]; done; echo $[ $l / 1024 / 1024 / 1024 ].$[ $l /1024 / 1024 % 1024] Gb

# as root
sudo su

# grab the size and path to the largest overlay dir
du /var/lib/docker/overlay2 -h | sort -h | tail -n 100 | grep -vE "overlay2$" > large-overlay.txt

# construct mappings of name to hash
docker inspect $(docker ps -qa) | jq -r 'map([.Name, .GraphDriver.Data.MergedDir]) | .[] | "\(.[0])\t\(.[1])"' > docker-mappings.txt

# for each hashed path, find matching container name
cat large-overlay.txt | xargs -l bash -c 'if grep $1 docker-mappings.txt; then echo -n "$0 "; fi'

sudo journalctl -n 50 -f -u docker-<cont_name>

sudo virsh list --all
sudo virsh undefine <вм_которую_удалить_окончательно!>
sudo virsh shutdown nyc-1-udf-proxy
sudo virsh dumpxml nyc-1-staging-backend-dashboard

sudo grep -iC 10 "killed" /var/log/kern.log
sudo grep -iC 10 "killed" /var/log/kern.log | grep docker

sudo cluster edit nyc-compute-25-vector
sudo systemctl daemon-reload 
sudo cluster restart nyc-compute-25-vector
sudo attach-docker nyc-compute-25-vector
sudo docker update --memory 2000m  nyc-compute-25-vector
sudo cluster edit nyc-compute-25-vector
sudo systemctl daemon-reload 
sudo cluster restart nyc-compute-25-vector
sudo docker update --memory 2000m  nyc-compute-25-vector
sudo attach-docker nyc-compute-25-vector
sudo docker update --memory 2500m  nyc-compute-25-vector
curl localhost:7890/tasks
