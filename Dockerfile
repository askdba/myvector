ARG MYSQL_VERSION=8.0
FROM mysql:${MYSQL_VERSION}
ARG TARGETARCH

# Copy the MyVector plugin and installation script
# MySQL images use /usr/lib64/mysql/plugin on some distros.
COPY myvector-${TARGETARCH}.so /usr/lib/mysql/plugin/myvector.so
RUN if [ -d /usr/lib64/mysql/plugin ]; then \
      cp /usr/lib/mysql/plugin/myvector.so /usr/lib64/mysql/plugin/; \
    fi
COPY myvectorplugin.sql /docker-entrypoint-initdb.d/

HEALTHCHECK --interval=30s --timeout=5s --retries=5 CMD \
  mysqladmin ping -uroot -p"$MYSQL_ROOT_PASSWORD" || exit 1

USER mysql

# The rest will be handled by the default MySQL entrypoint
