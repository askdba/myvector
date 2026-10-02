#!/bin/bash
# Docker initdb hook for the demo container (scripts/record-demo.sh).
#
# initdb scripts run in name order, so this runs before the image's
# myvector_install_component.sql. The component reads myvector.cnf from
# mysqld's working directory (the datadir). It uses it to connect back for
# index builds and to follow the binlog for online (online=Y) index updates.
#
# Demo only: it reuses the root password. For a real setup, use a dedicated
# user (see docs/ONLINE_INDEX_UPDATES.md).
cat >/var/lib/mysql/myvector.cnf <<CNF
myvector_host=127.0.0.1
myvector_port=3306
myvector_user_id=root
myvector_user_password=${MYSQL_ROOT_PASSWORD}
CNF
chmod 600 /var/lib/mysql/myvector.cnf
