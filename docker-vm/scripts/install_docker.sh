#!/bin/bash -e

curl -fsSL https://get.docker.com -o get-docker.sh; sh get-docker.sh
sed -i '/ExecStart/s/usr\/bin\/dockerd/usr\/bin\/dockerd --mtu=1450/' /lib/systemd/system/docker.service
sed -i '/ExecStart/ s/$/ -H=tcp:\/\/0.0.0.0:2375 --dns 8.8.8.8/' /lib/systemd/system/docker.service
usermod -aG docker $USER

if [ -f /etc/redhat-release ]; then
  systemctl daemon-reload
  systemctl restart docker.service
fi

if [ -f /etc/lsb-release ]; then
  if dpkg -l | grep systemd
  then
     echo "OK";
  else
     apt-get install -y systemd
  fi

  systemctl daemon-reload
  systemctl restart docker.service

fi
