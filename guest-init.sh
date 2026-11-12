#!/busybox sh
export PATH=/bin:/usr/bin:/sbin:/usr/sbin
/busybox --install -s /bin
ln -sf /busybox-extras /bin/httpd
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
mkdir -p /run /scratch /web /metrics/cgi-bin
mount -t tmpfs -o size=8m tmpfs /scratch
modprobe e1000
for attempt in 1 2 3 4 5; do
    [ -e /sys/class/net/eth0 ] && break
    sleep 1
done
ip link set lo up
ip addr add 10.0.2.15/24 dev eth0
ip link set eth0 up
echo 'nameserver 10.0.2.2' > /etc/resolv.conf
echo 'fleet service healthy' > /web/index.html
httpd -f -p 8080 -h /web &
echo $! > /run/service.pid
cat > /metrics/cgi-bin/metrics <<'EOF'
#!/bin/sh
printf 'Content-Type: text/plain; version=0.0.4\r\n\r\n'
if wget -T 1 -q -O /dev/null http://127.0.0.1:8080/; then
    echo 'lab_service_up 1'
else
    echo 'lab_service_up 0'
fi
df -P /scratch | awk 'NR==2 {sub(/%/, "", $5); print "lab_disk_used_percent " $5}'
if timeout 1 nslookup -type=A health.fleet.test 10.0.2.2 >/dev/null 2>&1; then
    echo 'lab_dns_up 1'
else
    echo 'lab_dns_up 0'
fi
EOF
chmod +x /metrics/cgi-bin/metrics
httpd -p 9100 -h /metrics
stty -echo
echo 'LAB_READY'
exec /bin/sh
