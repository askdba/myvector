# Use MySQL 8.0 as the base image
ARG MYSQL_VERSION=8.0
FROM mysql:${MYSQL_VERSION}

# Copy the MyVector plugin and installation script
COPY myvector.so /usr/lib/mysql/plugin/
COPY myvectorplugin.sql /docker-entrypoint-initdb.d/

# The rest will be handled by the default MySQL entrypoint
