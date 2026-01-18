# Use MySQL 8.0 as the base image
FROM mysql:8.0

# Copy the MyVector plugin and installation script
COPY myvector.so /usr/lib/mysql/plugin/
COPY myvectorplugin.sql /docker-entrypoint-initdb.d/

# The rest will be handled by the default MySQL entrypoint
