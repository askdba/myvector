ARG MYSQL_VERSION=8.0
ARG MYVECTOR_SO=myvector.so
FROM mysql:${MYSQL_VERSION}
ARG MYVECTOR_SO

# Copy the MyVector plugin and installation script
# MySQL images use /usr/lib64/mysql/plugin on some distros.
COPY ${MYVECTOR_SO} /usr/lib/mysql/plugin/myvector.so
RUN if [ -d /usr/lib64/mysql/plugin ]; then \
      cp /usr/lib/mysql/plugin/myvector.so /usr/lib64/mysql/plugin/; \
    fi
COPY myvectorplugin.sql /docker-entrypoint-initdb.d/

# The rest will be handled by the default MySQL entrypoint
