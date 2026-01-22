# Use MySQL 8.0 as the base image
ARG MYSQL_VERSION=8.0
FROM mysql:${MYSQL_VERSION}

# Copy the MyVector plugin and installation script
# MySQL images use /usr/lib64/mysql/plugin on some distros.
COPY myvector.so /usr/lib/mysql/plugin/
COPY myvector.so /usr/lib64/mysql/plugin/
COPY myvectorplugin.sql /docker-entrypoint-initdb.d/

# The rest will be handled by the default MySQL entrypoint
